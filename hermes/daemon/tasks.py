import time
import asyncio
from datetime import datetime
from typing import List, Tuple
from hermes.logging_setup import log
from hermes.state import state, prices, _multi_rsi_cache
from hermes.config import (
    ALL_TRACKED, MAX_ACTIVE_PAIRS, MIN_ACTIVE_PAIRS,
    ANALYSIS_REASSESS_INTERVAL, DAEMON_TRADE_CHECK_INTERVAL, DAEMON_FG_FETCH_INTERVAL,
    DAEMON_REBALANCE_INTERVAL, WS_PAIRS, FG_BUY_THRESHOLD, MIN_TRADE_USDT, MAX_TRADE_USDT,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, USE_STRATEGY_V2, DCA_CHECK_INTERVAL,
    DCA_TRIGGER_PCT, DCA_AMOUNT_PCT, DCA_MAX_COUNT, DCA_COOLDOWN_MINUTES,
    DCA_ACTIVE_PAIRS
)
from hermes.api.websocket import ws_client
from hermes.indicators.signals import get_daily_position, get_signal, get_market_regime, get_regime_trading_config
from hermes.indicators.rsi import get_rsi, get_multi_rsi_async
from hermes.indicators.fear_greed import fetch_fear_greed
from hermes.indicators.strategy_new import get_signal_v2_async, StrategyV2
from hermes.trading.positions import check_open_positions
from hermes.trading.execution import execute_buy
from hermes.trading.dca import DCAConfig, run_dca

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

            # Check regime change
            regime, _ = get_market_regime()
            if regime != state._last_regime:
                cfg = get_regime_trading_config()
                from hermes.notifications.telegram import telegram_regime_alert
                telegram_regime_alert(regime, state.fg_value, cfg["max_active_multiplier"], cfg["position_size_mult"])
                state._last_regime = regime

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
            usdt = balance.get("usdt", 0)
            
            if usdt < MIN_TRADE_USDT and not state.positions:
                continue
            
            # Check positions using WS prices (no REST)
            for pair in list(state.positions.keys()):
                if pair in prices:
                    pair_price = prices[pair].get("price", 0)
                    if pair_price > 0:
                        check_open_positions(pair_price, balance, specific_pair=pair)
            
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
                        if usdt >= MIN_TRADE_USDT:
                            live_balance = get_balance_func(use_cache=False)
                            usdt = live_balance.get("usdt", 0)
                            if usdt >= MIN_TRADE_USDT and execute_buy(pair, price, usdt):
                                usdt -= MAX_TRADE_USDT
            
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
            usdt = balance.get("usdt", 0)
            
            # 1) Always check open positions using WS prices (no REST)
            for pair in list(state.positions.keys()):
                if pair in prices:
                    pair_price = prices[pair].get("price", 0)
                    if pair_price > 0:
                        check_open_positions(pair_price, balance, specific_pair=pair)

            # 2) Always monitor Autonomous Futures Position & Exit (TP / SL / Trailing)
            from hermes.trading.futures_monitor import check_open_futures_positions
            check_open_futures_positions()

            # 3) Check if enough capital exists for new entry (Spot or Capital Rotation)
            can_rotate = bool(getattr(state, "positions", {}))
            if usdt < MIN_TRADE_USDT and not can_rotate:
                continue

            # Fast Evaluation on Volatility Spike
            spike_pairs = [p for p, ts in getattr(state, "spike_events", {}).items() if time.time() - ts < 20]
            if spike_pairs:
                for pair in spike_pairs:
                    if pair in prices and pair not in state.positions and usdt >= MIN_TRADE_USDT:
                        price = prices[pair]["price"]
                        res = await get_signal_v2_async(pair, capital=usdt, risk_pct=0.01)
                        signal_data = res.get("signal", res) if isinstance(res, dict) else {}
                        if signal_data.get("signal_type") == "LONG":
                            log.info(f"[SPIKE-TRADE] Fast Execution on {pair} Spike! Price: ${price}")
                            live_balance = get_balance_func(use_cache=False)
                            usdt = live_balance.get("usdt", 0)
                            if usdt >= MIN_TRADE_USDT and execute_buy(pair, price, usdt):
                                usdt -= MAX_TRADE_USDT
            
            # Regime-aware trading gatekeeper (BULL/SIDEWAYS/BEAR)
            # Bullish/Greed (FG >= 50): Aggressive momentum & breakout allowed
            # Neutral/Fear (FG < 50): Dip buying enabled, filter extreme crash if FG < 10
            if state.fg_value >= 15:
                for pair in state.active_pairs:
                    if pair not in prices:
                        continue
                    if pair in state.positions:
                        continue
                    
                    if state.last_trade_time.get(pair, 0) > time.time() - TRADE_COOLDOWN:
                        continue
                    
                    price = prices[pair]["price"]
                    
                    # Run Strategy V2 analysis
                    res = await get_signal_v2_async(pair, capital=usdt, risk_pct=0.01)
                    signal_data = res.get("signal", res) if isinstance(res, dict) else {}
                    signal_type = signal_data.get("signal_type", "NO TRADE SETUP")
                    confidence = signal_data.get("signal_confidence", "Low")
                    conf_level = confidence_order.get(confidence, 0)
                    
                    # Only execute if signal and confidence meet threshold
                    if signal_type == "LONG" and conf_level >= min_conf_level:
                        # Safety check: News sentiment & emergency circuit breaker
                        try:
                            from hermes.indicators.news_sentiment import is_news_safe_to_buy
                            safe, news_reason = is_news_safe_to_buy(pair)
                            if not safe:
                                log.warning(f"[V2-TRADE] 🛡️ Skipping {pair.upper()} LONG due to news risk: {news_reason}")
                                continue
                        except Exception as ne:
                            log.debug(f"[V2-TRADE] News sentiment check skipped: {ne}")

                        # 1) Try Spot Buy first if Spot USDT >= MIN_TRADE_USDT
                        # Always use live balance here to avoid stale cache causing shadow block to be skipped
                        live_balance = get_balance_func(use_cache=False)
                        usdt = live_balance.get("usdt", 0)
                        score = signal_data.get("score", 8 if confidence == "High" else 6)
                        if usdt >= MIN_TRADE_USDT:
                            log.info(f"[V2-TRADE] {pair.upper()}: LONG signal ({confidence}) at ${price} on SPOT")
                            log.info(f"         RSI: {signal_data.get('rsi_value', 0):.1f} | DailyPos: {signal_data.get('daily_position', 0):.1f}%")
                            log.info(f"         SL: ${signal_data.get('stop_loss', 0):.4f} | TP1: ${signal_data.get('take_profit_1', 0):.4f} | TP2: ${signal_data.get('take_profit_2', 0):.4f}")
                            if usdt >= MIN_TRADE_USDT:
                                buy_ok, buy_msg = execute_buy(pair, price, usdt, confidence=confidence, score=score)
                                if buy_ok:
                                    usdt = max(0.0, usdt - MIN_TRADE_USDT)
                        else:
                            # 2) If Spot USDT insufficient, evaluate cost-aware rotation in SHADOW mode (zero order mutations)
                            from hermes.accounting.schema import init_db
                            from hermes.accounting.repository import SqliteRotationRepository
                            from hermes.accounting.rotation import (
                                ShadowRotationRecorder,
                                RotationPolicyConfig,
                                RotationMarketSnapshot,
                            )
                            from hermes.accounting.contracts import (
                                Completeness,
                                Venue,
                                canonical_decimal,
                            )
                            from hermes.config import (
                                ACCOUNTING_DB_PATH,
                                ROTATION_ENABLED,
                                ROTATION_SHADOW_MODE,
                                ROTATION_SCORE_DELTA,
                                ROTATION_MIN_PNL_PCT,
                                ROTATION_MAX_PNL_PCT,
                                ROTATION_MIN_HOLD_SECS,
                                ROTATION_COOLDOWN_SECS,
                            )
                            import hermes.config as cfg

                            rot_enabled = getattr(cfg, "ROTATION_ENABLED", ROTATION_ENABLED)
                            rot_shadow = getattr(cfg, "ROTATION_SHADOW_MODE", ROTATION_SHADOW_MODE)
                            db_path = getattr(cfg, "ACCOUNTING_DB_PATH", ACCOUNTING_DB_PATH)
                            init_db(db_path)

                            score = signal_data.get("score", 8 if confidence == "High" else 6)
                            now_ms = int(time.time() * 1000)

                            cand_snapshot = RotationMarketSnapshot(
                                snapshot_id=f"snap_cand_{pair.upper()}_{now_ms}",
                                account_id="default",
                                venue=Venue.SPOT,
                                symbol=pair.upper(),
                                position_lifecycle_id=None,
                                position_version=None,
                                model_version="v2",
                                feature_schema_version="v1",
                                observed_at_ms=now_ms,
                                expires_at_ms=now_ms + 60000,
                                score=canonical_decimal(str(score), non_negative=True),
                                best_bid_usdt=canonical_decimal(str(price), non_negative=True),
                                best_ask_usdt=canonical_decimal(str(price), non_negative=True),
                                spread_fraction="0.001",
                                available_depth_usdt="1000",
                                closed_bar_ids=(f"{pair}_bar",),
                                completeness=Completeness.VERIFIED,
                            )

                            rot_policy_config = RotationPolicyConfig(
                                model_version="v2",
                                feature_schema_version="v1",
                                enabled=rot_enabled,
                                shadow_mode=rot_shadow,
                                min_score_edge=canonical_decimal(str(getattr(cfg, "ROTATION_SCORE_DELTA", ROTATION_SCORE_DELTA)), non_negative=True),
                                min_pnl_pct=canonical_decimal(str(getattr(cfg, "ROTATION_MIN_PNL_PCT", ROTATION_MIN_PNL_PCT))),
                                max_pnl_pct=canonical_decimal(str(getattr(cfg, "ROTATION_MAX_PNL_PCT", ROTATION_MAX_PNL_PCT))),
                                min_hold_secs=getattr(cfg, "ROTATION_MIN_HOLD_SECS", ROTATION_MIN_HOLD_SECS),
                                cooldown_secs=getattr(cfg, "ROTATION_COOLDOWN_SECS", ROTATION_COOLDOWN_SECS),
                            )

                            rotation_repo = SqliteRotationRepository(db_path)
                            recorder = ShadowRotationRecorder(rotation_repo=rotation_repo, config=rot_policy_config)

                            for held_sym, held_pos in list(state.positions.items()):
                                if held_sym.upper() == pair.upper():
                                    continue
                                h_entry = held_pos.get("entry_price", 0.0)
                                h_qty = held_pos.get("qty", 0.0)
                                h_time = held_pos.get("time", 0.0)
                                h_mode = held_pos.get("mode", "NORMAL")
                                h_curr_price = prices.get(held_sym.upper(), {}).get("price", h_entry)
                                h_pnl = (h_curr_price - h_entry) / h_entry if h_entry > 0 else 0.0
                                h_hold_secs = int(time.time() - h_time)
                                h_notional = h_qty * h_curr_price

                                from hermes.trading.rotation import get_position_momentum_score
                                h_score, _ = get_position_momentum_score(held_sym, h_curr_price)

                                held_snapshot = RotationMarketSnapshot(
                                    snapshot_id=f"snap_held_{held_sym.upper()}_{now_ms}",
                                    account_id="default",
                                    venue=Venue.SPOT,
                                    symbol=held_sym.upper(),
                                    position_lifecycle_id=f"pos_{held_sym.upper()}",
                                    position_version=1,
                                    model_version="v2",
                                    feature_schema_version="v1",
                                    observed_at_ms=now_ms,
                                    expires_at_ms=now_ms + 60000,
                                    score=canonical_decimal(str(h_score), non_negative=True),
                                    best_bid_usdt=canonical_decimal(str(h_curr_price), non_negative=True),
                                    best_ask_usdt=canonical_decimal(str(h_curr_price), non_negative=True),
                                    spread_fraction="0.001",
                                    available_depth_usdt="1000",
                                    closed_bar_ids=(f"{held_sym}_bar",),
                                    completeness=Completeness.VERIFIED,
                                )

                                rot_decision = recorder.record_evaluation(
                                    held_snapshot=held_snapshot,
                                    candidate_snapshot=cand_snapshot,
                                    held_position_pnl_pct=canonical_decimal(str(h_pnl)),
                                    held_holding_time_secs=h_hold_secs,
                                    now_ms=now_ms,
                                    held_mode=h_mode,
                                    position_lifecycle_id=f"pos_{held_sym.upper()}",
                                    position_version=1,
                                    position_notional_usdt=canonical_decimal(str(max(5.5, h_notional)), non_negative=True),
                                )
                                log.info(
                                    f"[ROTATION-SHADOW] Evaluated {held_sym.upper()} -> {pair.upper()}: "
                                    f"action={rot_decision.action.value}, edge={rot_decision.score_edge}, "
                                    f"benefit=${rot_decision.expected_net_benefit_usdt}, reasons={rot_decision.reason_codes}"
                                )

                            log.info(f"[V2-TRADE] {pair.upper()}: LONG signal ({confidence}) at ${price} (Spot USDT < ${MIN_TRADE_USDT:.2f}, shadow rotation recorded). Auto Futures fallback disabled.")
                    
                    elif signal_type == "SHORT" and conf_level >= min_conf_level:
                        from hermes.config import FUTURES_ENABLED
                        if not FUTURES_ENABLED:
                            log.info(f"[V2-TRADE] {pair.upper()}: SHORT signal ({confidence}) at ${price} skipped (Futures trading is disabled by default).")
                        else:
                            from hermes.trading.futures import get_futures_account_overview, execute_futures_order
                            f_acc = get_futures_account_overview()
                            f_avail = f_acc.get("available_balance", 0.0) if f_acc.get("success") else 0.0
                            if f_avail >= 5.0:
                                trade_margin = min(f_avail, 15.0) # max $15 margin per short
                                log.info(f"[FUTURES-AUTO-SHORT] {pair.upper()}: Executing SHORT on Futures (${trade_margin:.2f} margin, 3x) at ${price}")
                                execute_futures_order(pair=pair, side="SHORT", usdt_margin=trade_margin, leverage=3)
                            else:
                                log.info(f"[V2-TRADE] {pair.upper()}: SHORT signal ({confidence}) at ${price} (Futures balance < $5, skipped)")
            
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
        log.info(f"USDT Balance: ${balance.get('usdt', 0):,.2f}")
        
        telegram_morning_brief(state.fg_value, state.fg_class, balance.get('usdt', 0), state.positions)
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


def telegram_dca_alert(pair: str, dca_count: int, qty: float, price: float):
    """Send DCA execution alert to Telegram."""
    from hermes.notifications.telegram import telegram_send
    msg = (
        f"📈 *DCA Buy Executed!*\n"
        f"Pair: {pair.upper()}\n"
        f"DCA #: {dca_count}\n"
        f"Qty: {qty:,.8f} @ Rp {price:,.0f}"
    )
    telegram_send(msg)


async def daemon_dca(get_balance_func):
    """Periodically check DCA conditions for active pairs."""
    while True:
        await asyncio.sleep(DCA_CHECK_INTERVAL)
        try:
            balance = get_balance_func(use_cache=True)
            usdt = balance.get("usdt", 0)

            if usdt < MIN_TRADE_USDT:
                continue

            dca_config = DCAConfig(
                trigger_pct=DCA_TRIGGER_PCT,
                amount_pct=DCA_AMOUNT_PCT,
                max_dca_count=DCA_MAX_COUNT,
                cooldown_minutes=DCA_COOLDOWN_MINUTES,
            )

            for pair in DCA_ACTIVE_PAIRS:
                if pair not in state.positions:
                    continue
                if pair not in prices:
                    continue

                price = prices[pair]["price"]
                result = run_dca(pair, dca_config, usdt, price)

                if result["triggered"] and result["action"] == "buy":
                    log.info(f"[DCA] Executed DCA for {pair.upper()}: "
                             f"count={result['dca_count']}, new_entry={result['new_entry']:,.0f}")
                    new_pos = state.positions.get(pair)
                    if new_pos:
                        telegram_dca_alert(
                            pair,
                            result["dca_count"],
                            new_pos.get("qty", 0),
                            result["new_entry"]
                        )
                    usdt -= MAX_TRADE_USDT

        except Exception as e:
            log.error(f"[DCA] Daemon error: {e}")

def validate_positions_on_startup(get_balance_func):
    """Reconcile and synchronize positions from Binance myTrades and Spot balance on startup.

    Ensures all active assets in Spot have exact entry prices and TP/SL tracking,
    syncs SQLite accounting ledger, and removes stale positions with 0 balance.
    """
    try:
        from hermes.accounting.schema import init_db
        from hermes.accounting.repository import SqliteAccountingRepository
        from hermes.accounting.contracts import AccountingCutover, CutoverStatus, Venue
        from hermes.accounting.ingestion import sync_accounting_trades
        from hermes.config import ACCOUNTING_DB_PATH
        import hermes.config as cfg

        db_path = getattr(cfg, "ACCOUNTING_DB_PATH", ACCOUNTING_DB_PATH)
        init_db(db_path)
        repo = SqliteAccountingRepository(db_path)

        # Wire snapshot creation with initial PENDING state (cutover must not self-approve on flag toggle)
        con = repo._con()
        try:
            cur = con.execute("SELECT cutover_id FROM accounting_cutovers WHERE account_id = 'default' AND venue = 'SPOT' LIMIT 1;")
            if not cur.fetchone():
                now_ms = int(time.time() * 1000)
                repo.create_cutover(
                    AccountingCutover(
                        cutover_id="cutover_genesis",
                        account_id="default",
                        venue=Venue.SPOT,
                        cutover_at_ms=now_ms,
                        baseline_reference="genesis_init",
                        backfill_from_ms=None,
                        backfill_through_ms=now_ms,
                        status=CutoverStatus.PENDING,
                        approved_at_ms=None,
                    )
                )
        finally:
            con.close()

        sync_accounting_trades(repo=repo)
    except Exception as e:
        log.error(f"[STARTUP-SYNC] Error initializing accounting ledger on startup: {e}")

    try:
        from hermes.trading.reconcile import reconcile_positions_from_binance
        reconciled = reconcile_positions_from_binance(save_to_state=True)
        log.info(f"[STARTUP-SYNC] Synchronized {len(reconciled)} open positions from Binance myTrades.")
    except Exception as e:
        log.error(f"[STARTUP-SYNC] Error reconciling positions on startup: {e}")


async def daemon_periodic_sync(get_balance_func=None):
    """Periodically reconcile open positions and accounting ledger from Binance Spot balances & myTrades every 10 minutes."""
    while True:
        try:
            await asyncio.sleep(600)  # Reconcile every 10 minutes
            from hermes.trading.reconcile import reconcile_positions_from_binance
            loop = asyncio.get_running_loop()
            reconciled = await loop.run_in_executor(None, reconcile_positions_from_binance, 5.0, True)
            log.info(f"[PERIODIC-SYNC] Background sync complete. Active positions: {len(reconciled)}")

            # Ingest paginated Binance myTrades into authoritative SQLite accounting repository
            try:
                from hermes.accounting.ingestion import sync_accounting_trades
                await loop.run_in_executor(None, sync_accounting_trades)
            except Exception as ase:
                log.error(f"[PERIODIC-SYNC] Error syncing accounting trades: {ase}")

            # SpotToFunding daily profit collector check (sweeps target amount once reached)
            try:
                from hermes.api.transfer import run_daily_profit_collector
                await loop.run_in_executor(None, run_daily_profit_collector, get_balance_func)
            except Exception as dpe:
                log.error(f"[PERIODIC-SYNC] Daily profit collector error: {dpe}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"[PERIODIC-SYNC] Error during periodic sync: {e}")


async def run_daemon(get_balance_func, dry_run: bool = False):
    """Run the trading daemon."""
    state.dry_run = dry_run
    log.info("Hermes Trader Daemon starting...")
    log.info(f"Strategy: F&G + RSI + Daily Position + Dynamic Pair Selection")
    log.info(f"Tracked pairs: {len(ALL_TRACKED)} ({', '.join(ALL_TRACKED)})")
    log.info(f"Max active pairs: {MAX_ACTIVE_PAIRS} | Reassess every: {ANALYSIS_REASSESS_INTERVAL}s")
    log.info(f"Max trade: Rp {MAX_TRADE_USDT:,} | Stop Loss: {STOP_LOSS_PCT*100:.0f}% | Take Profit: {TAKE_PROFIT_PCT*100:.0f}%")
    if dry_run:
        log.info("DRY_RUN MODE — No real orders will be executed")

    # Fetch F&G first
    fetch_fear_greed()
    log.info(f"Fear & Greed: {state.fg_value} ({state.fg_class})")

    # Validate positions against Binance — remove stale entries before trading starts
    validate_positions_on_startup(get_balance_func)
    
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
    # period=3 requires >= 2 ticks to set initialized=True; use 15 ticks for stability
    from hermes.indicators.rsi import update_rsi
    for pair, data in prices.items():
        if data.get("source") in ("ws", "ws_summary", "ws_orderbook"):
            for _ in range(15):
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
        daemon_rebalance(get_balance_func),
        daemon_dca(get_balance_func),
        daemon_periodic_sync(get_balance_func)
    )
