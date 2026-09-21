"""Futures Autonomous Position Monitor & Auto-Exit Management with Dynamic TP."""

import time
from typing import Dict, List, Any
from hermes.logging_setup import log
from hermes.trading.futures import get_futures_account_overview, close_futures_position
from hermes.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_ACTIVATION_PCT, TRAILING_STOP_PCT
from hermes.trading.tp_evaluator import evaluate_tp_momentum, send_tp_extension_alert

# Track highest/lowest PnL reached per position in memory
_futures_peaks: Dict[str, float] = {}
# Track dynamic riding trend states for futures: {pos_key: {"mode": "RIDING_TREND", "floor_roe": 0.08, "trail_pct": 0.035, "tp_evaluated": True}}
_futures_riding: Dict[str, Dict[str, Any]] = {}

def check_open_futures_positions() -> None:
    """Monitor active Binance Futures positions and trigger autonomous Dynamic TP/SL/Trailing Stop.
    
    Rules:
    - SL: -5.0% on margin (STOP_LOSS_PCT)
    - TP Checkpoint: +10.0% ROE on margin -> AI & Momentum re-evaluation!
      - If EXTEND_AND_RIDE: locks floor at +8.0% ROE and trails up to +20%, +50%, +100% ROE.
      - If TAKE_PROFIT_NOW: closes immediately at +10% ROE.
    - Pre-TP Trailing Stop: Starts at +6.0% gain (TRAILING_ACTIVATION_PCT), closes if profit drops 2.5% from peak (TRAILING_STOP_PCT).
    """
    try:
        overview = get_futures_account_overview()
        if not overview.get("success"):
            return

        positions = overview.get("open_positions", [])
        if not positions:
            _futures_peaks.clear()
            _futures_riding.clear()
            return

        for pos in positions:
            symbol = pos.get("symbol", "")
            pair = pos.get("pair", "")
            side = pos.get("side", "LONG")
            amount = pos.get("amount", 0.0)
            entry_price = pos.get("entry_price", 0.0)
            unrealized_pnl = pos.get("unrealized_pnl", 0.0)
            leverage = pos.get("leverage", 3)

            if entry_price <= 0 or amount <= 0:
                continue

            # Initial Margin used
            notional = amount * entry_price
            initial_margin = notional / leverage if leverage > 0 else notional

            # Return on Equity (ROE %)
            roe_pct = (unrealized_pnl / initial_margin) if initial_margin > 0 else 0.0

            pos_key = f"{symbol}_{side}"
            peak_roe = _futures_peaks.get(pos_key, roe_pct)
            if roe_pct > peak_roe:
                peak_roe = roe_pct
                _futures_peaks[pos_key] = peak_roe

            riding_state = _futures_riding.get(pos_key)

            # ─── 1. DYNAMIC RIDING TREND MODE (Futures) ──────────────────────
            if riding_state and riding_state.get("mode") == "RIDING_TREND":
                floor_roe = riding_state.get("floor_roe", 0.08)

                # Ratchet up the profit floor for Futures as ROE climbs
                if roe_pct >= 0.50 and floor_roe < 0.40:
                    riding_state["floor_roe"] = 0.40
                    log.info(f"🚀 [FUTURES-RATCHET] {pos_key}: Floor ROE raised to +40.0%")
                elif roe_pct >= 0.30 and floor_roe < 0.25:
                    riding_state["floor_roe"] = 0.25
                    log.info(f"🚀 [FUTURES-RATCHET] {pos_key}: Floor ROE raised to +25.0%")
                elif roe_pct >= 0.20 and floor_roe < 0.15:
                    riding_state["floor_roe"] = 0.15
                    log.info(f"🚀 [FUTURES-RATCHET] {pos_key}: Floor ROE raised to +15.0%")

                trail_pct = riding_state.get("trail_pct", 0.035)
                trail_trigger_roe = peak_roe - trail_pct
                effective_floor = max(riding_state.get("floor_roe", 0.08), trail_trigger_roe)

                if roe_pct <= effective_floor:
                    reason = f"Dynamic Futures TP Exit (ROE +{roe_pct*100:.1f}%, Peak: +{peak_roe*100:.1f}%)"
                    log.info(f"🎯 [FUTURES-DYNAMIC-TP] {pos_key}: Hit Dynamic Exit at ROE +{roe_pct*100:.2f}% (Peak +{peak_roe*100:.2f}%)")
                    success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                    if success:
                        try:
                            from hermes.notifications.telegram import telegram_futures_exit_alert
                            exit_price = entry_price + (unrealized_pnl / amount) if side.upper() == "LONG" else entry_price - (unrealized_pnl / amount)
                            telegram_futures_exit_alert(
                                pair=pair, side=side, entry_price=entry_price, exit_price=exit_price,
                                amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                                initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type="TP",
                                peak_roe=peak_roe
                            )
                        except Exception as te:
                            log.debug(f"Telegram exit alert error: {te}")
                        _futures_peaks.pop(pos_key, None)
                        _futures_riding.pop(pos_key, None)
                    continue

            # ─── 2. PRIMARY TAKE PROFIT CHECKPOINT (+10.0% ROE) ──────────────
            elif roe_pct >= TAKE_PROFIT_PCT:
                if pos_key not in _futures_riding:
                    log.info(f"🎯 [FUTURES-CHECKPOINT] {pos_key} reached +{roe_pct*100:.1f}% ROE! Running AI Evaluation...")
                    current_est_price = entry_price + (unrealized_pnl / amount) if side.upper() == "LONG" else entry_price - (unrealized_pnl / amount)
                    evaluation = evaluate_tp_momentum(
                        pair=pair,
                        current_price=current_est_price,
                        entry_price=entry_price,
                        pnl_pct=roe_pct,
                        is_futures=True,
                        leverage=leverage
                    )

                    if evaluation.get("action") == "EXTEND_AND_RIDE":
                        floor_roe = evaluation.get("guaranteed_floor_pct", 0.08)
                        trail_pct = evaluation.get("recommended_trail_pct", 0.035)
                        _futures_riding[pos_key] = {
                            "mode": "RIDING_TREND",
                            "floor_roe": floor_roe,
                            "trail_pct": trail_pct,
                            "tp_evaluated": True
                        }
                        log.info(f"🚀 [FUTURES-EXTENDED] {pos_key}: Trend Extension activated! Floor ROE: +{floor_roe*100:.1f}%")

                        try:
                            send_tp_extension_alert(
                                pair=pair,
                                current_price=current_est_price,
                                entry_price=entry_price,
                                pnl_pct=roe_pct,
                                floor_price=entry_price * (1 + (floor_roe / leverage) if side.upper() == "LONG" else 1 - (floor_roe / leverage)),
                                floor_pct=floor_roe,
                                trail_pct=trail_pct,
                                reason=evaluation.get("reason", ""),
                                sentiment_str=evaluation.get("sentiment_summary", ""),
                                is_futures=True,
                                leverage=leverage
                            )
                        except Exception as te:
                            log.debug(f"Futures extension alert error: {te}")
                        continue
                    else:
                        reason = f"Take Profit (+{roe_pct*100:.1f}% ROE - {evaluation.get('reason', '')})"
                        log.info(f"🎯 [FUTURES-TP] {pos_key}: Hit TP at ROE +{roe_pct*100:.2f}% (PnL: ${unrealized_pnl:+.2f})")
                        success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                        if success:
                            try:
                                from hermes.notifications.telegram import telegram_futures_exit_alert
                                exit_price = entry_price + (unrealized_pnl / amount) if side.upper() == "LONG" else entry_price - (unrealized_pnl / amount)
                                telegram_futures_exit_alert(
                                    pair=pair, side=side, entry_price=entry_price, exit_price=exit_price,
                                    amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                                    initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type="TP"
                                )
                            except Exception as te:
                                log.debug(f"Telegram exit alert error: {te}")
                            _futures_peaks.pop(pos_key, None)
                            _futures_riding.pop(pos_key, None)
                        continue

            # ─── 3. PRE-TP TRAILING STOP (ROE >= +6.0%) ──────────────────────
            elif peak_roe >= TRAILING_ACTIVATION_PCT and pos_key not in _futures_riding:
                if roe_pct <= (peak_roe - TRAILING_STOP_PCT):
                    reason = f"Trailing Stop (Peak +{peak_roe*100:.1f}%)"
                    log.info(f"🛡️ [FUTURES-TRAIL] {pos_key}: Trailing Stop hit! Current ROE +{roe_pct*100:.2f}% (Peak: +{peak_roe*100:.2f}%)")
                    success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                    if success:
                        try:
                            from hermes.notifications.telegram import telegram_futures_exit_alert
                            exit_price = entry_price + (unrealized_pnl / amount) if side.upper() == "LONG" else entry_price - (unrealized_pnl / amount)
                            telegram_futures_exit_alert(
                                pair=pair, side=side, entry_price=entry_price, exit_price=exit_price,
                                amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                                initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type="TRAIL",
                                peak_roe=peak_roe
                            )
                        except Exception as te:
                            log.debug(f"Telegram exit alert error: {te}")
                        _futures_peaks.pop(pos_key, None)
                        _futures_riding.pop(pos_key, None)
                    continue

            # ─── 4. HARD STOP LOSS (-5.0% ROE) ───────────────────────────────
            if roe_pct <= -STOP_LOSS_PCT and pos_key not in _futures_riding:
                reason = f"Stop Loss ({roe_pct*100:.1f}%)"
                log.info(f"🛑 [FUTURES-SL] {pos_key}: Stop Loss hit at ROE {roe_pct*100:.2f}% (PnL: ${unrealized_pnl:+.2f})")
                success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                if success:
                    try:
                        from hermes.notifications.telegram import telegram_futures_exit_alert
                        exit_price = entry_price + (unrealized_pnl / amount) if side.upper() == "LONG" else entry_price - (unrealized_pnl / amount)
                        telegram_futures_exit_alert(
                            pair=pair, side=side, entry_price=entry_price, exit_price=exit_price,
                            amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                            initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type="SL"
                        )
                    except Exception as te:
                        log.debug(f"Telegram exit alert error: {te}")
                    _futures_peaks.pop(pos_key, None)
                    _futures_riding.pop(pos_key, None)
                continue

    except Exception as e:
        log.error(f"[FUTURES-MONITOR-ERROR] {e}")
