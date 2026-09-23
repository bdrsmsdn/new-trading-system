import time
from typing import Dict, Optional
from hermes.logging_setup import log
from hermes.state import state, _multi_rsi_cache
from hermes.indicators.rsi import get_rsi, get_multi_rsi
from hermes.indicators.signals import get_signal
from hermes.trading.execution import execute_sell, execute_buy
from hermes.api.balance import get_balance
from hermes.config import (
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_ACTIVATION_PCT,
    TRAILING_STOP_PCT, TRADE_COOLDOWN, MIN_TRADE_USDT
)
from hermes.trading.tp_evaluator import evaluate_tp_momentum, send_tp_extension_alert
from hermes.utils import format_price

_MULTI_RSI_TTL = 300  # Match rsi.py TTL

def check_open_positions(current_price: float, balance: Dict[str, float], specific_pair: Optional[str] = None) -> None:
    """Check and manage open positions (Dynamic TP / SL / Trailing Stop / AI Re-evaluation).
    Uses cached RSI only — no heavy REST calls for position checks."""
    pairs_to_check = [specific_pair] if (specific_pair and specific_pair in state.positions) else list(state.positions.keys())
    for pair in pairs_to_check:
        pos = state.positions.get(pair)
        if not pos:
            continue
        entry = pos["entry_price"]
        qty = pos.get("qty", 0)
        stop_loss = pos.get("stop_loss", entry * (1 - STOP_LOSS_PCT))
        peak_price = pos.get("peak_price", entry)
        
        pnl_pct = (current_price - entry) / entry
        
        # Update peak price if new high reached
        if current_price > peak_price:
            peak_price = current_price
            pos["peak_price"] = peak_price

        # Check mode: Is position currently in RIDING_TREND mode?
        is_riding = pos.get("mode") == "RIDING_TREND"

        # ─── 1. DYNAMIC RIDING TREND MODE (Post-10% Evaluation) ───────────
        if is_riding:
            current_floor_pct = pos.get("profit_floor_pct", 0.08)

            # Ratchet up the profit floor as price climbs higher!
            if pnl_pct >= 0.40 and current_floor_pct < 0.35:
                pos["profit_floor_pct"] = 0.35
                log.info(f"🚀 [FLOOR-RATCHET] {pair}: Profit floor raised to +35.0%")
                state.save()
            elif pnl_pct >= 0.30 and current_floor_pct < 0.25:
                pos["profit_floor_pct"] = 0.25
                log.info(f"🚀 [FLOOR-RATCHET] {pair}: Profit floor raised to +25.0%")
                state.save()
            elif pnl_pct >= 0.20 and current_floor_pct < 0.15:
                pos["profit_floor_pct"] = 0.15
                log.info(f"🚀 [FLOOR-RATCHET] {pair}: Profit floor raised to +15.0%")
                state.save()

            floor_price = entry * (1 + pos.get("profit_floor_pct", 0.08))
            trail_pct = pos.get("trailing_stop_pct", 0.035)
            trailing_stop_price = peak_price * (1 - trail_pct)
            effective_exit_price = max(floor_price, trailing_stop_price)

            if current_price <= effective_exit_price:
                peak_pnl_pct = (peak_price - entry) / entry
                log.info(f"🎯 [DYNAMIC-TP-EXIT] {pair}: Closed at +{pnl_pct*100:.1f}% (Peak: +{peak_pnl_pct*100:.1f}%)")
                execute_sell(pair, current_price, qty, f"Dynamic TP Exit (Peak +{peak_pnl_pct*100:.1f}%)", order_type="market")
                continue

        # ─── 2. PRIMARY TAKE PROFIT CHECKPOINT (+10.0%) ─────────────────────
        elif pnl_pct >= TAKE_PROFIT_PCT:
            if not pos.get("tp_evaluated", False):
                pos["tp_evaluated"] = True
                log.info(f"🎯 [TP-CHECKPOINT] {pair} reached +{pnl_pct*100:.1f}%! Running Dynamic Momentum & AI Evaluation...")
                
                evaluation = evaluate_tp_momentum(
                    pair=pair,
                    current_price=current_price,
                    entry_price=entry,
                    pnl_pct=pnl_pct,
                    is_futures=False
                )

                if evaluation.get("action") == "EXTEND_AND_RIDE":
                    pos["mode"] = "RIDING_TREND"
                    pos["profit_floor_pct"] = evaluation.get("guaranteed_floor_pct", 0.08)
                    pos["trailing_stop_pct"] = evaluation.get("recommended_trail_pct", 0.035)
                    pos["evaluation_reason"] = evaluation.get("reason", "")
                    state.save()

                    floor_price = entry * (1 + pos["profit_floor_pct"])
                    log.info(f"🚀 [TP-EXTENDED] {pair}: Trend Extension activated! Floor: +{pos['profit_floor_pct']*100:.1f}% ({format_price(floor_price)})")

                    try:
                        send_tp_extension_alert(
                            pair=pair,
                            current_price=current_price,
                            entry_price=entry,
                            pnl_pct=pnl_pct,
                            floor_price=floor_price,
                            floor_pct=pos["profit_floor_pct"],
                            trail_pct=pos["trailing_stop_pct"],
                            reason=evaluation.get("reason", ""),
                            sentiment_str=evaluation.get("sentiment_summary", ""),
                            is_futures=False
                        )
                    except Exception as te:
                        log.debug(f"Extension alert error: {te}")
                    continue
                else:
                    reason_text = evaluation.get("reason", "Take profit secured at +10%")
                    log.info(f"🎯 [TP-EXIT] {pair}: Taking immediate profit at +{pnl_pct*100:.1f}% ({reason_text})")
                    execute_sell(pair, current_price, qty, f"Take Profit (+{pnl_pct*100:.1f}%)", order_type="market")
                    continue
            else:
                # Already evaluated as take profit
                log.info(f"TP hit for {pair}: +{pnl_pct*100:.1f}%")
                execute_sell(pair, current_price, qty, f"Take Profit (+{pnl_pct*100:.1f}%)", order_type="market")
                continue

        # ─── 3. PRE-TP TRAILING STOP (+6.0% ~ +9.9%) ────────────────────────
        elif pnl_pct >= TRAILING_ACTIVATION_PCT:
            trailing_stop_price = peak_price * (1 - TRAILING_STOP_PCT)
            if current_price <= trailing_stop_price:
                log.info(f"Trailing SL hit for {pair}: price dropped to {current_price}, trail price {trailing_stop_price}, peak {peak_price}")
                execute_sell(pair, current_price, qty, "Trailing Stop", order_type="market")
                continue

        # ─── 4. STANDARD STOP LOSS (-5.0%) ──────────────────────────────────
        if current_price <= stop_loss:
            log.info(f"SL hit for {pair}: {pnl_pct*100:.1f}%")
            execute_sell(pair, current_price, qty, "Stop Loss", order_type="market")
            continue

        # ─── 5. SIGNAL-BASED EARLY EXIT ─────────────────────────────────────
        # Only trigger if profit is already substantial (>= +2.0%) or severe reversal
        if pnl_pct >= 0.02 and not is_riding:
            cached_mrsi = _multi_rsi_cache.get(pair, {})
            if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
                multi_rsi = dict(cached_mrsi["rsi"])
                multi_rsi["3m"] = get_rsi(pair)
            else:
                multi_rsi = {"3m": get_rsi(pair), "1h": 50.0, "4h": 50.0}
            signal, score, reasons = get_signal(pair, current_price, multi_rsi)
            if signal == "STRONG_SELL":
                coin = pair.replace("USDT", "")
                balances = get_balance(use_cache=False)
                coin_lower = coin.lower()
                balance = balances.get(coin_lower, 0)
                if balance == 0:
                    log.warning(f"Stale position {pair} — Binance balance is 0, skipping sell and removing from state")
                    del state.positions[pair]
                    state.save()
                    continue
                log.info(f"Signal exit for {pair}: {signal} with +{pnl_pct*100:.2f}%")
                execute_sell(pair, current_price, qty, f"Signal: {signal}")

def check_for_entries(pair: str, current_price: float, idr_balance: float, dry_run: bool = False) -> bool:
    """Check if we should enter a position. Budget-aware."""
    last_trade = state.last_trade_time.get(pair, 0)
    if time.time() - last_trade < TRADE_COOLDOWN:
        return False
    
    if pair in state.positions:
        return False
    
    from hermes.api.rest import _check_budget
    cached_mrsi = _multi_rsi_cache.get(pair, {})
    if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
        multi_rsi = dict(cached_mrsi["rsi"])
        multi_rsi["3m"] = get_rsi(pair)
    elif _check_budget():
        multi_rsi = get_multi_rsi(pair, current_price)
    else:
        multi_rsi = {"3m": get_rsi(pair), "1h": 50.0, "4h": 50.0}

    signal, score, reasons = get_signal(pair, current_price, multi_rsi)
    
    log.info(f"{pair.upper()}: {signal} (score={score}) - {', '.join(reasons)}")
    
    if signal in ["STRONG_BUY", "BUY"] and idr_balance >= MIN_TRADE_USDT:
        return execute_buy(pair, current_price, idr_balance, dry_run=dry_run)
    
    return False
