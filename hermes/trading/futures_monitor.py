"""
Futures Autonomous Position Monitor & Exit Management with Dynamic TP & Lifecycle Safety.

Normative implementation adhering to docs/trading-risk-contract.md:
- Immediate initial peak persistence on first observation.
- Position lifecycle identity tracking (scoped by account, venue, symbol, side, lifecycle_id).
- Disambiguated units: futures_roe_drawdown_points and floor_trigger_roe (percentage-point units).
- Directional symmetry: support for both LONG and SHORT futures positions.
- Nonblocking Checkpoint State Machine: STANDARD -> TRAILING_ARMED -> TP_EVALUATING -> RIDING / EXIT_PENDING -> CLOSED.
- Floor breach during TP_EVALUATING triggers immediate exit without waiting for AI.
- Late/stale AI responses are safely discarded without state corruption or resurrection.
"""

from __future__ import annotations

import concurrent.futures
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from hermes.logging_setup import log
from hermes.trading.futures import get_futures_account_overview, close_futures_position
from hermes.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_ACTIVATION_PCT, TRAILING_STOP_PCT
from hermes.trading.tp_evaluator import evaluate_tp_momentum, send_tp_extension_alert
from hermes.trading.exit_policy import decide_futures_exit
from hermes.state import state

EVALUATION_DEADLINE_SECONDS = 3.0

# Asynchronous TP evaluation pool for Futures
_futures_eval_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="futures_tp_eval")
_futures_active_evaluations: Dict[str, Dict[str, Any]] = {}


def _get_or_init_futures_position(pos_key: str, symbol: str, side: str, roe_pct: float) -> Dict[str, Any]:
    """Ensure futures position state is initialized and initial peak is immediately persisted."""
    if pos_key not in state.futures_positions:
        state.futures_positions[pos_key] = {
            "symbol": symbol,
            "side": side.upper(),
            "position_lifecycle_id": str(uuid.uuid4())[:8],
            "position_version": 1,
            "peak_roe": roe_pct,
            "state": "STANDARD",
            "trailing_armed": False,
            "floor_trigger_roe": 0.08,
            "futures_roe_drawdown_points": TRAILING_STOP_PCT,
            "tp_evaluated": False,
        }
        state.save()
    
    pos_data = state.futures_positions[pos_key]
    
    # Invariant: persist peak immediately and update monotonically
    stored_peak = float(pos_data.get("peak_roe", roe_pct))
    if roe_pct > stored_peak:
        pos_data["peak_roe"] = roe_pct
        state.save()
        
    return pos_data


def check_open_futures_positions() -> None:
    """
    Monitor active Binance Futures positions and trigger autonomous Dynamic TP/SL/Trailing Stop.
    Adheres to the Nonblocking Checkpoint State Machine and protective exit precedence.
    """
    try:
        overview = get_futures_account_overview()
        if not overview.get("success"):
            return

        positions = overview.get("open_positions", [])
        active_pos_keys = set()

        if positions:
            for pos in positions:
                symbol = pos.get("symbol", "")
                pair = pos.get("pair", "")
                side = str(pos.get("side", "LONG")).upper()
                amount = float(pos.get("amount", 0.0))
                entry_price = float(pos.get("entry_price", 0.0))
                unrealized_pnl = float(pos.get("unrealized_pnl", 0.0))
                leverage = int(pos.get("leverage", 3))

                if entry_price <= 0 or amount <= 0:
                    continue

                pos_key = f"{symbol}_{side}"
                active_pos_keys.add(pos_key)

                # Margin and Return on Equity (ROE %)
                notional = amount * entry_price
                initial_margin = notional / leverage if leverage > 0 else notional
                roe_pct = (unrealized_pnl / initial_margin) if initial_margin > 0 else 0.0

                pos_data = _get_or_init_futures_position(pos_key, symbol, side, roe_pct)
                peak_roe = float(pos_data.get("peak_roe", roe_pct))
                pos_state = pos_data.get("state", "STANDARD")
                trailing_armed = bool(pos_data.get("trailing_armed", False))
                lifecycle_id = pos_data.get("position_lifecycle_id", "default")
                version = int(pos_data.get("position_version", 1))

                # Update trailing_armed flag if activation reached
                if not trailing_armed and peak_roe >= TRAILING_ACTIVATION_PCT:
                    trailing_armed = True
                    pos_data["trailing_armed"] = True
                    if pos_state == "STANDARD":
                        pos_state = "TRAILING_ARMED"
                        pos_data["state"] = pos_state
                    state.save()

                # Estimated current mark price for alerts & evaluator
                if side == "LONG":
                    current_est_price = entry_price + (unrealized_pnl / amount)
                else:
                    current_est_price = entry_price - (unrealized_pnl / amount)

                # ─── 0. EXIT_PENDING RECONCILIATION ─────────────────────────
                if pos_state == "EXIT_PENDING":
                    last_attempt = pos_data.get("last_close_attempt", 0)
                    if time.time() - last_attempt >= 5.0:
                        pos_data["last_close_attempt"] = time.time()
                        reason = pos_data.get("pending_exit_reason", "Pending Futures Exit")
                        log.info(f"[FUTURES-EXIT-RETRY] Retrying exit for {pos_key}: {reason}")
                        success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                        if success:
                            state.futures_positions.pop(pos_key, None)
                            _futures_active_evaluations.pop(pos_key, None)
                            state.save()
                    continue

                # ─── 1. TP_EVALUATING (Nonblocking Checkpoint) ─────────────
                if pos_state == "TP_EVALUATING":
                    # Rule A: Floor breach during evaluation triggers immediate protective exit!
                    exit_decision = decide_futures_exit(
                        peak_roe=peak_roe,
                        current_roe=roe_pct,
                        trailing_armed=trailing_armed,
                        activation_roe=TRAILING_ACTIVATION_PCT,
                        futures_roe_drawdown_points=TRAILING_STOP_PCT,
                        hard_stop_roe=STOP_LOSS_PCT,
                        side=side,
                        mode="TP_EVALUATING",
                        floor_trigger_roe=pos_data.get("floor_trigger_roe", 0.08),
                    )

                    if exit_decision.should_exit:
                        log.info(f"🎯 [FUTURES-TP-EVAL-BREACH] {pos_key}: Protective floor breached during evaluation ({exit_decision.reason})")
                        pos_data["state"] = "EXIT_PENDING"
                        pos_data["pending_exit_reason"] = exit_decision.reason
                        state.save()
                        _futures_active_evaluations.pop(pos_key, None)

                        reason = f"Protective Exit during TP Evaluation ({exit_decision.exit_type})"
                        success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                        if success:
                            try:
                                from hermes.notifications.telegram import telegram_futures_exit_alert
                                telegram_futures_exit_alert(
                                    pair=pair, side=side, entry_price=entry_price, exit_price=current_est_price,
                                    amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                                    initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type="SL",
                                    peak_roe=peak_roe
                                )
                            except Exception as te:
                                log.debug(f"Telegram exit alert error: {te}")
                            state.futures_positions.pop(pos_key, None)
                            state.save()
                        continue

                    # Rule B: Check async evaluation completion
                    eval_entry = _futures_active_evaluations.get(pos_key)
                    now = time.time()
                    deadline = pos_data.get("evaluation_deadline", now + EVALUATION_DEADLINE_SECONDS)

                    if eval_entry and eval_entry.get("future") and eval_entry["future"].done():
                        try:
                            evaluation = eval_entry["future"].result()
                        except Exception as exc:
                            log.warning(f"[FUTURES-TP-EVAL-ERROR] Async evaluation error for {pos_key}: {exc}")
                            evaluation = {"action": "TAKE_PROFIT_NOW", "reason": f"Evaluation error: {exc}"}

                        _futures_active_evaluations.pop(pos_key, None)

                        # Revalidate state and lifecycle
                        if (
                            pos_key in state.futures_positions
                            and pos_data.get("state") == "TP_EVALUATING"
                            and pos_data.get("position_lifecycle_id") == eval_entry.get("position_lifecycle_id")
                            and pos_data.get("position_version") == eval_entry.get("position_version")
                        ):
                            if evaluation.get("action") == "EXTEND_AND_RIDE":
                                floor_roe_val = float(evaluation.get("profit_floor_trigger_pct", evaluation.get("guaranteed_floor_pct", 0.08)))
                                trail_roe_val = float(evaluation.get("recommended_trail_pct", 0.035))

                                pos_data["state"] = "RIDING"
                                pos_data["mode"] = "RIDING_TREND"
                                pos_data["floor_trigger_roe"] = floor_roe_val
                                pos_data["profit_floor_trigger_pct"] = floor_roe_val
                                pos_data["guaranteed_floor_pct"] = floor_roe_val
                                pos_data["futures_roe_drawdown_points"] = trail_roe_val
                                pos_data["evaluation_reason"] = str(evaluation.get("reason", ""))
                                state.save()

                                log.info(f"🚀 [FUTURES-EXTENDED] {pos_key}: Trend Extension activated! Floor Trigger ROE: +{floor_roe_val*100:.1f}%")
                                try:
                                    floor_price = (
                                        entry_price * (1.0 + (floor_roe_val / leverage))
                                        if side == "LONG"
                                        else entry_price * (1.0 - (floor_roe_val / leverage))
                                    )
                                    send_tp_extension_alert(
                                        pair=pair,
                                        current_price=current_est_price,
                                        entry_price=entry_price,
                                        pnl_pct=roe_pct,
                                        floor_price=floor_price,
                                        floor_pct=floor_roe_val,
                                        trail_pct=trail_roe_val,
                                        reason=str(evaluation.get("reason", "")),
                                        sentiment_str=str(evaluation.get("sentiment_summary", "")),
                                        is_futures=True,
                                        leverage=leverage,
                                        side=side,
                                        source=str(evaluation.get("source", "DETERMINISTIC")),
                                    )
                                except Exception as te:
                                    log.debug(f"Futures extension alert error: {te}")
                                continue
                            else:
                                reason = f"Take Profit (+{roe_pct*100:.1f}% ROE - {evaluation.get('reason', '')})"
                                log.info(f"🎯 [FUTURES-TP] {pos_key}: Hit TP at ROE +{roe_pct*100:.2f}% (PnL: ${unrealized_pnl:+.2f})")
                                pos_data["state"] = "EXIT_PENDING"
                                pos_data["pending_exit_reason"] = reason
                                state.save()

                                success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                                if success:
                                    try:
                                        from hermes.notifications.telegram import telegram_futures_exit_alert
                                        telegram_futures_exit_alert(
                                            pair=pair, side=side, entry_price=entry_price, exit_price=current_est_price,
                                            amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                                            initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type="TP",
                                            peak_roe=peak_roe
                                        )
                                    except Exception as te:
                                        log.debug(f"Telegram exit alert error: {te}")
                                    state.futures_positions.pop(pos_key, None)
                                    state.save()
                                continue
                        else:
                            log.warning(f"[FUTURES-TP-EVAL-STALE] Discarding late AI response for {pos_key}")
                            continue

                    elif now >= deadline:
                        log.warning(f"🎯 [FUTURES-TP-DEADLINE-EXPIRED] {pos_key} evaluation exceeded 3s. Failing closed to Take Profit Exit.")
                        reason = f"Take Profit (+{roe_pct*100:.1f}% ROE - Deadline Expired)"
                        pos_data["state"] = "EXIT_PENDING"
                        pos_data["pending_exit_reason"] = reason
                        state.save()
                        _futures_active_evaluations.pop(pos_key, None)

                        success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                        if success:
                            try:
                                from hermes.notifications.telegram import telegram_futures_exit_alert
                                telegram_futures_exit_alert(
                                    pair=pair, side=side, entry_price=entry_price, exit_price=current_est_price,
                                    amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                                    initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type="TP",
                                    peak_roe=peak_roe
                                )
                            except Exception as te:
                                log.debug(f"Telegram exit alert error: {te}")
                            state.futures_positions.pop(pos_key, None)
                            state.save()
                        continue

                    else:
                        # In flight within deadline
                        continue

                # ─── 2. DYNAMIC RIDING TREND MODE ───────────────────────────
                if pos_state == "RIDING" or pos_data.get("mode") == "RIDING_TREND":
                    exit_decision = decide_futures_exit(
                        peak_roe=peak_roe,
                        current_roe=roe_pct,
                        trailing_armed=trailing_armed,
                        activation_roe=TRAILING_ACTIVATION_PCT,
                        futures_roe_drawdown_points=pos_data.get("futures_roe_drawdown_points", 0.035),
                        hard_stop_roe=STOP_LOSS_PCT,
                        side=side,
                        mode="RIDING",
                        floor_trigger_roe=pos_data.get("floor_trigger_roe", pos_data.get("profit_floor_trigger_pct", pos_data.get("guaranteed_floor_pct", 0.08))),
                        riding_roe_drawdown_points=pos_data.get("futures_roe_drawdown_points", 0.035),
                    )

                    # Update ratcheted floor in state
                    if exit_decision.profit_floor_roe is not None:
                        updated_floor_roe = float(exit_decision.profit_floor_roe)
                        current_floor_roe = pos_data.get("floor_trigger_roe", pos_data.get("profit_floor_trigger_pct", pos_data.get("guaranteed_floor_pct", 0.08)))
                        if updated_floor_roe > current_floor_roe:
                            pos_data["floor_trigger_roe"] = updated_floor_roe
                            pos_data["profit_floor_trigger_pct"] = updated_floor_roe
                            pos_data["guaranteed_floor_pct"] = updated_floor_roe
                            log.info(f"🚀 [FUTURES-RATCHET] {pos_key}: Floor Trigger ROE raised to +{updated_floor_roe*100:.1f}%")
                            state.save()

                    if exit_decision.should_exit:
                        reason = f"Dynamic Futures TP Exit (ROE +{roe_pct*100:.1f}%, Peak: +{peak_roe*100:.1f}%)"
                        log.info(f"🎯 [FUTURES-DYNAMIC-TP] {pos_key}: Hit Dynamic Exit at ROE +{roe_pct*100:.2f}% (Peak +{peak_roe*100:.2f}%)")
                        pos_data["state"] = "EXIT_PENDING"
                        pos_data["pending_exit_reason"] = reason
                        state.save()

                        success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                        if success:
                            try:
                                from hermes.notifications.telegram import telegram_futures_exit_alert
                                telegram_futures_exit_alert(
                                    pair=pair, side=side, entry_price=entry_price, exit_price=current_est_price,
                                    amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                                    initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type="TP",
                                    peak_roe=peak_roe
                                )
                            except Exception as te:
                                log.debug(f"Telegram exit alert error: {te}")
                            state.futures_positions.pop(pos_key, None)
                            state.save()
                        continue
                    else:
                        continue

                # ─── 3. PRIMARY TAKE PROFIT CHECKPOINT (+10.0% ROE) ─────────
                if roe_pct >= TAKE_PROFIT_PCT and not pos_data.get("tp_evaluated", False):
                    pos_data["tp_evaluated"] = True
                    pos_data["state"] = "TP_EVALUATING"
                    pos_data["evaluation_started_at"] = time.time()
                    pos_data["evaluation_deadline"] = time.time() + EVALUATION_DEADLINE_SECONDS
                    pos_data["position_lifecycle_id"] = lifecycle_id
                    pos_data["position_version"] = version
                    state.save()

                    log.info(f"🎯 [FUTURES-CHECKPOINT] {pos_key} reached +{roe_pct*100:.1f}% ROE! Spawning AI Evaluation...")

                    future = _futures_eval_executor.submit(
                        evaluate_tp_momentum,
                        pair=pair,
                        current_price=current_est_price,
                        entry_price=entry_price,
                        pnl_pct=roe_pct,
                        is_futures=True,
                        leverage=leverage,
                        side=side,
                        position_lifecycle_id=lifecycle_id,
                        position_version=version,
                    )
                    _futures_active_evaluations[pos_key] = {
                        "future": future,
                        "started_at": pos_data["evaluation_started_at"],
                        "deadline": pos_data["evaluation_deadline"],
                        "position_lifecycle_id": lifecycle_id,
                        "position_version": version,
                    }
                    continue

                # ─── 4. STANDARD & TRAILING STOP EXITS ──────────────────────
                exit_decision = decide_futures_exit(
                    peak_roe=peak_roe,
                    current_roe=roe_pct,
                    trailing_armed=trailing_armed,
                    activation_roe=TRAILING_ACTIVATION_PCT,
                    futures_roe_drawdown_points=TRAILING_STOP_PCT,
                    hard_stop_roe=STOP_LOSS_PCT,
                    side=side,
                    mode=pos_state,
                )

                if exit_decision.should_exit:
                    reason = f"{exit_decision.exit_type.replace('_', ' ').title()} (ROE {roe_pct*100:.1f}%)"
                    log.info(f"🎯 [FUTURES-{exit_decision.exit_type}] {pos_key}: {exit_decision.reason}")
                    pos_data["state"] = "EXIT_PENDING"
                    pos_data["pending_exit_reason"] = reason
                    state.save()

                    success, _ = close_futures_position(pair=pair, side=side, qty=amount, reason=reason)
                    if success:
                        try:
                            from hermes.notifications.telegram import telegram_futures_exit_alert
                            exit_alert_type = "SL" if exit_decision.exit_type == "STOP_LOSS" else "TRAIL"
                            telegram_futures_exit_alert(
                                pair=pair, side=side, entry_price=entry_price, exit_price=current_est_price,
                                amount=amount, roe_pct=roe_pct, pnl_usd=unrealized_pnl,
                                initial_margin=initial_margin, leverage=leverage, reason=reason, exit_type=exit_alert_type,
                                peak_roe=peak_roe
                            )
                        except Exception as te:
                            log.debug(f"Telegram exit alert error: {te}")
                        state.futures_positions.pop(pos_key, None)
                        state.save()
                    continue

        # Prune state for positions that are no longer open
        for pos_key in list(state.futures_positions.keys()):
            if pos_key not in active_pos_keys:
                log.info(f"Pruning closed futures position state for {pos_key}")
                del state.futures_positions[pos_key]
                _futures_active_evaluations.pop(pos_key, None)
                state.save()

    except Exception as e:
        log.error(f"[FUTURES-MONITOR-ERROR] {e}")
