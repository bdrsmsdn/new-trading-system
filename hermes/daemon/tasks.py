import time
import asyncio
from datetime import datetime
from typing import List, Tuple
from hermes.logging_setup import log
from hermes.state import state, prices, _multi_rsi_cache
from hermes.config import (
    ALL_TRACKED, MAX_ACTIVE_PAIRS, MIN_ACTIVE_PAIRS,
    ANALYSIS_REASSESS_INTERVAL, DAEMON_TRADE_CHECK_INTERVAL, DAEMON_FG_FETCH_INTERVAL,
    DAEMON_REBALANCE_INTERVAL, WS_PAIRS, FG_BUY_THRESHOLD, MIN_TRADE_RP, MAX_TRADE_RP,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, USE_STRATEGY_V2
)
from hermes.api.websocket import ws_client
from hermes.indicators.signals import get_daily_position, get_signal, get_market_regime, get_regime_trading_config
from hermes.indicators.rsi import get_rsi, get_multi_rsi_async
from hermes.indicators.fear_greed import fetch_fear_greed
from hermes.indicators.strategy_new import get_signal_v2_async, StrategyV2
from hermes.trading.positions import check_open_positions
from hermes.trading.execution import execute_buy

# ── Configuration ──
_MULTI_RSI_TTL = 300  # Match rsi.py TTL
_RANK_BATCH_SIZE = 10  # Only refresh RSI for top N pairs per rank cycle
_RANK_STAGGER_SLEEP = 2.0  # Seconds between pairs during ranking

async def rank_all_pairs() -> List[Tuple[str, int, str, float]]:
    """Score and rank ALL tracked pairs for trading priority.
    Only refreshes multi-RSI for a limited batch to stay within REST budget."""
    from hermes.api.rest import _check_budget

    rankings = []
    fresh_rsi_count = 0

    for pair in ALL_TRACKED:
        if pair not in prices:
            continue
        price_data = prices[pair]
        price = price_data.get("price", 0)
        if not price or price <= 0:
            continue

        # Always try cached multi-RSI first
        cached_mrsi = _multi_rsi_cache.get(pair, {})
        if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
            multi_rsi = dict(cached_mrsi["rsi"])
            multi_rsi["3m"] = get_rsi(pair)
        elif fresh_rsi_count < _RANK_BATCH_SIZE and _check_budget():
            # Only fetch fresh RSI for limited pairs, and only if budget allows
            multi_rsi = await get_multi_rsi_async(pair, price)
            fresh_rsi_count += 1
            await asyncio.sleep(_RANK_STAGGER_SLEEP)  # Non-blocking stagger between REST calls
        else:
            # No cache, no budget — use 3m RSI only (from WS price updates)
            multi_rsi = {"3m": get_rsi(pair), "1h": 50.0, "4h": 50.0}

        daily_pos = get_daily_position(pair, price)  # Now WS-only, no REST
        signal, score, reasons = get_signal(pair, price, multi_rsi)

        rankings.append((pair, score, signal, daily_pos))

    rankings.sort(key=lambda x: x[1], reverse=True)
    log.info(f"[RANK] Ranked {len(rankings)} pairs (fresh RSI for {fresh_rsi_count})")
    return rankings

async def update_active_pairs() -> Tuple[List[str], List[Tuple[str, int, str, float]]]:
    """Auto-adjust active_pairs based on current signal scores + market regime."""
    locked = set(state.positions.keys())
    regime_cfg = get_regime_trading_config()
    
    effective_max = int(MAX_ACTIVE_PAIRS * regime_cfg["max_active_multiplier"])
    effective_max = max(effective_max, MIN_ACTIVE_PAIRS)
    
    rankings = await rank_all_pairs()

    new_active = list(locked)
    slots = effective_max - len(locked)

    if slots > 0:
        for pair, score, signal, daily_pos in rankings:
            if pair in locked:
                continue
            min_score = 3 if regime_cfg["max_active_multiplier"] < 0.8 else 1
            if score >= min_score or (len(new_active) < MIN_ACTIVE_PAIRS and score >= 0):
                new_active.append(pair)
                slots -= 1
                if slots <= 0:
                    break

    if len(new_active) < MIN_ACTIVE_PAIRS:
        for pair, score, signal, daily_pos in rankings:
            if pair not in new_active:
                new_active.append(pair)
                if len(new_active) >= MIN_ACTIVE_PAIRS:
                    break

    locked_list = [p for p in new_active if p in locked]
    unlocked_list = [p for p in new_active if p not in locked]

    unlocked_sorted = []
    for pair in unlocked_list:
        for r_pair, score, signal, daily_pos in rankings:
            if r_pair == pair:
                unlocked_sorted.append((pair, score))
                break
    unlocked_sorted.sort(key=lambda x: x[1], reverse=True)
    new_active = locked_list + [p for p, _ in unlocked_sorted]

    changed = set(new_active) != set(state.active_pairs)
    if changed:
        regime, _ = get_market_regime()
        log.info(f"[PAIR-ADJUST] Active pairs: {len(new_active)} ({regime} mode) — {', '.join(new_active)}")

    return new_active, rankings

async def daemon_pair_reassess():
    """Periodically re-rank all pairs and update active_pairs."""
    while True:
        await asyncio.sleep(ANALYSIS_REASSESS_INTERVAL)
        try:
            new_active, rankings = await update_active_pairs()
            state.active_pairs = new_active
            state.save()

            log.info(f"[PAIR-RANK] Top 5: " + " | ".join(
                f"{p}:{s}({sig})" for p, s, sig, _ in rankings[:5]
            ))
        except Exception as e:
            log.error(f"[PAIR-REASSESS] Error: {e}")

async def daemon_trade_check(get_balance_func):
    """Periodically check for trade opportunities. Uses cached/WS data primarily.
    
    This uses the original F&G+RSI strategy. For Strategy V2, use daemon_trade_check_v2().
    """
    while True:
        await asyncio.sleep(DAEMON_TRADE_CHECK_INTERVAL)
        try:
            balance = get_balance_func(use_cache=True)
            idr = balance.get("idr", 0)
            
            if idr < MIN_TRADE_RP and not state.positions:
                continue
            
            # Check positions using WS prices (no REST)
            for pair in list(state.positions.keys()):
                if pair in prices:
                    check_open_positions(prices[pair]["price"], balance)
            
            # Only look for entries when F&G is favorable
            if state.fg_value <= FG_BUY_THRESHOLD:
                for pair in state.active_pairs:
                    if pair not in prices:
                        continue
                    if pair in state.positions:
                        continue
                    
                    from hermes.config import TRADE_COOLDOWN
                    if state.last_trade_time.get(pair, 0) > time.time() - TRADE_COOLDOWN:
                        continue
                    
                    price = prices[pair]["price"]
                    # Use cached multi-RSI — no fresh REST calls
                    cached_mrsi = _multi_rsi_cache.get(pair, {})
                    if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
                        multi_rsi = dict(cached_mrsi["rsi"])
                        multi_rsi["3m"] = get_rsi(pair)
                    else:
                        # If no cache, use WS-derived RSI only (don't fetch candles)
                        multi_rsi = {"3m": get_rsi(pair), "1h": 50.0, "4h": 50.0}
                    
                    signal, score, reasons = get_signal(pair, price, multi_rsi)
                    
                    if signal in ["STRONG_BUY", "BUY"]:
                        if idr >= MIN_TRADE_RP:
                            live_balance = get_balance_func(use_cache=False)
                            idr = live_balance.get("idr", 0)
                            if idr >= MIN_TRADE_RP and execute_buy(pair, price, idr):
                                idr -= MAX_TRADE_RP
            
            state.save()
        except Exception as e:
            log.error(f"Trade check error: {e}")


async def daemon_trade_check_v2(get_balance_func, min_confidence: str = "Medium"):
    """Periodically check for trade opportunities using Strategy V2.
    
    Strategy V2 uses RSI + EMA crossover + Orderbook analysis.
    Requires candle data so uses more REST budget than the original strategy.
    
    Args:
        get_balance_func: Function to get balance
        min_confidence: Minimum signal confidence to execute ("Low", "Medium", "High")
    """
    from hermes.api.orderbook import get_orderbook_async
    from hermes.config import TRADE_COOLDOWN
    
    confidence_order = {"Low": 0, "Medium": 1, "High": 2}
    min_conf_level = confidence_order.get(min_confidence, 1)
    
    while True:
        await asyncio.sleep(DAEMON_TRADE_CHECK_INTERVAL)
        try:
            balance = get_balance_func(use_cache=True)
            idr = balance.get("idr", 0)
            
            if idr < MIN_TRADE_RP and not state.positions:
                continue
            
            # Check positions using WS prices (no REST)
            for pair in list(state.positions.keys()):
                if pair in prices:
                    check_open_positions(prices[pair]["price"], balance)
            
            # Only look for entries when F&G is favorable
            if state.fg_value <= FG_BUY_THRESHOLD:
                for pair in state.active_pairs:
                    if pair not in prices:
                        continue
                    if pair in state.positions:
                        continue
                    
                    if state.last_trade_time.get(pair, 0) > time.time() - TRADE_COOLDOWN:
                        continue
                    
                    price = prices[pair]["price"]
                    
                    # Run Strategy V2 analysis
                    signal_data = await get_signal_v2_async(pair, capital=idr, risk_pct=0.01)
                    signal_type = signal_data.get("signal_type", "NO TRADE SETUP")
                    confidence = signal_data.get("signal_confidence", "Low")
                    conf_level = confidence_order.get(confidence, 0)
                    
                    # Only execute if signal and confidence meet threshold
                    if signal_type == "LONG" and conf_level >= min_conf_level:
                        if idr >= MIN_TRADE_RP:
                            log.info(f"[V2-TRADE] {pair.upper()}: LONG signal ({confidence}) at Rp {price:,.0f}")
                            log.info(f"         RSI: {signal_data.get('rsi_value', 0):.1f} | EMA9: {signal_data.get('ema_9', 0):.4f} | EMA21: {signal_data.get('ema_21', 0):.4f}")
                            log.info(f"         SL: {signal_data.get('stop_loss', 0):,.0f} | TP3: {signal_data.get('take_profit_3', 0):,.0f}")
                            log.info(f"         Orderbook imbalance: {signal_data.get('orderbook_imbalance', 1.0):.2f}")
                            
                            live_balance = get_balance_func(use_cache=False)
                            idr = live_balance.get("idr", 0)
                            if idr >= MIN_TRADE_RP and execute_buy(pair, price, idr):
                                idr -= MAX_TRADE_RP
                    
                    elif signal_type == "SHORT" and conf_level >= min_conf_level:
                        # For shorts, we need to have the asset first
                        # This would be implemented with sell logic
                        log.info(f"[V2-TRADE] {pair.upper()}: SHORT signal ({confidence}) - not implemented in auto-trader")
            
            state.save()
        except Exception as e:
            log.error(f"V2 Trade check error: {e}")

async def daemon_fg_fetch():
    """Periodically fetch Fear & Greed."""
    while True:
        await asyncio.sleep(DAEMON_FG_FETCH_INTERVAL)
        fetch_fear_greed()

async def daemon_morning_brief(get_balance_func):
    """Send morning brief at 07:00 WIB."""
    from hermes.notifications.telegram import telegram_morning_brief
    while True:
        now = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        
        if now.hour >= 7:
            target = target.replace(day=now.day + 1)
        
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        
        log.info("\n" + "=" * 60)
        log.info(f"HERMES MORNING BRIEF — {datetime.now().strftime('%d %b %Y, %H:%M WIB')}")
        log.info("=" * 60)
        
        fetch_fear_greed()
        regime, _ = get_market_regime()
        
        balance = get_balance_func(use_cache=True)
        log.info(f"IDR Balance: Rp {balance.get('idr', 0):,.0f}")
        
        telegram_morning_brief(state.fg_value, state.fg_class, balance.get('idr', 0), state.positions)
        log.info("=" * 60)

async def daemon_rebalance(get_balance_func):
    """Periodically run portfolio drift check and rebalance every 6 hours."""
    from hermes.trading.rebalancer import run_rebalance
    while True:
        await asyncio.sleep(DAEMON_REBALANCE_INTERVAL)
        try:
            run_rebalance(get_balance_func)
        except Exception as e:
            log.error(f"[REBALANCE] Daemon error: {e}")

async def run_daemon(get_balance_func):
    """Run the trading daemon."""
    log.info("Hermes Trader Daemon starting...")
    log.info(f"Strategy: F&G + RSI + Daily Position + Dynamic Pair Selection")
    log.info(f"Tracked pairs: {len(ALL_TRACKED)} ({', '.join(ALL_TRACKED)})")
    log.info(f"Max active pairs: {MAX_ACTIVE_PAIRS} | Reassess every: {ANALYSIS_REASSESS_INTERVAL}s")
    log.info(f"Max trade: Rp {MAX_TRADE_RP:,} | Stop Loss: {STOP_LOSS_PCT*100:.0f}% | Take Profit: {TAKE_PROFIT_PCT*100:.0f}%")
    
    # Fetch F&G first (uses requests directly, not Indodax API)
    fetch_fear_greed()
    log.info(f"Fear & Greed: {state.fg_value} ({state.fg_class})")
    
    # Clear stale RSI cache and state from previous run
    _multi_rsi_cache.clear()
    for pair in state.rsi_state:
        state.rsi_state[pair] = {"avg_gain": 0, "avg_loss": 0, "last_price": 0, "initialized": False}

    # Start WS FIRST and wait for prices to populate
    log.info("Starting WebSocket feed for price initialization (wait 8s)...")
    ws_task = asyncio.create_task(ws_client.run_forever())
    await asyncio.sleep(8)
    log.info(f"WS parallel fill complete. Prices loaded for {len(prices)} pairs.")

    # Seed RSI state from WS prices so it's initialized before trade checks start
    from hermes.indicators.rsi import update_rsi
    for pair, data in prices.items():
        if data.get("source") in ("ws", "ws_summary", "ws_orderbook"):
            update_rsi(pair, data["price"])

    if not state.active_pairs:
        state.active_pairs, _ = await update_active_pairs()
        state.save()
    log.info(f"Active pairs: {', '.join(state.active_pairs)}")
    
    # Start all daemon tasks
    trade_check = daemon_trade_check_v2(get_balance_func) if USE_STRATEGY_V2 else daemon_trade_check(get_balance_func)
    strategy_name = "V2 (RSI + Orderbook + Momentum)" if USE_STRATEGY_V2 else "V1 (F&G + RSI + Daily Position)"
    log.info(f"Strategy: {strategy_name}")

    await asyncio.gather(
        ws_task,
        trade_check,
        daemon_fg_fetch(),
        daemon_pair_reassess(),
        daemon_morning_brief(get_balance_func),
        daemon_rebalance(get_balance_func)
    )
