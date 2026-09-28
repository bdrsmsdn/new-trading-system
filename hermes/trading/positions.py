"""
Position Management & Nonblocking Checkpoint State Machine.

Normative implementation adhering to docs/trading-risk-contract.md:
- State Machine: STANDARD -> TRAILING_ARMED -> TP_EVALUATING -> RIDING / EXIT_PENDING -> CLOSED.
- Nonblocking evaluation: 3.0s total evaluation deadline.
- Protective exits (SL, Trailing Stop, Floor Breach) strictly prioritize over AI and BUY signals.
- Invariant: armed trailing stop survives price retracement (e.g. 100 -> 106 -> 103).
- Late or stale AI evaluations arriving after position state/lifecycle change are safely discarded.
- Failed sells enter EXIT_PENDING without duplicate order flooding or state inconsistency.
"""

from __future__ import annotations

import concurrent.futures
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

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
from hermes.trading.exit_policy import decide_exit
from hermes.utils import format_price

_MULTI_RSI_TTL = 300  # Match rsi.py TTL
EVALUATION_DEADLINE_SECONDS = 3.0

# Asynchronous TP evaluation pool
_eval_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="tp_eval")
_active_evaluations: Dict[str, Dict[str, Any]] = {}


def _get_or_init_position_metadata(pair: str, pos: Dict[str, Any], current_price: float) -> Dict[str, Any]:
    """Ensure all required state machine and lifecycle fields exist on the position dict."""
    entry = pos.get("entry_price", current_price)
    if "peak_price" not in pos:
        pos["peak_price"] = entry
    if "state" not in pos:
        # Backwards compatibility check
        if pos.get("mode") == "RIDING_TREND":
            pos["state"] = "RIDING"
        else:
            pos["state"] = "STANDARD"
    if "trailing_armed" not in pos:
        pos["trailing_armed"] = False
    if "position_lifecycle_id" not in pos:
        pos["position_lifecycle_id"] = str(uuid.uuid4())[:8]
    if "position_version" not in pos:
        pos["position_version"] = 1
    return pos


def check_open_positions(current_price: float, balance: Dict[str, float], specific_pair: Optional[str] = None) -> None:
    """
    Check and manage open positions using the Nonblocking Checkpoint State Machine.
    Prioritizes protective exits over all other evaluations.
    """
    # Prune active evaluations for closed positions
    for eval_pair in list(_active_evaluations.keys()):
        if eval_pair not in state.positions:
            _active_evaluations.pop(eval_pair, None)

    pairs_to_check = [specific_pair] if (specific_pair and specific_pair in state.positions) else list(state.positions.keys())
    
    for pair in pairs_to_check:
        pos = state.positions.get(pair)
        if not pos:
            _active_evaluations.pop(pair, None)
            continue

        _get_or_init_position_metadata(pair, pos, current_price)

        entry = pos["entry_price"]
        qty = pos.get("qty", 0)
        peak_price = pos.get("peak_price", entry)
        pos_state = pos.get("state", "STANDARD")
        trailing_armed = pos.get("trailing_armed", False)
        lifecycle_id = pos.get("position_lifecycle_id", "default")
        version = pos.get("position_version", 1)

        pnl_pct = (current_price - entry) / entry if entry > 0 else 0.0

        # Update peak price monotonically for LONG
        if current_price > peak_price:
            peak_price = current_price
            pos["peak_price"] = peak_price

        # Update trailing_armed flag if activation reached
        if not trailing_armed and pnl_pct >= TRAILING_ACTIVATION_PCT:
            trailing_armed = True
            pos["trailing_armed"] = True
            if pos_state == "STANDARD":
                pos_state = "TRAILING_ARMED"
                pos["state"] = pos_state
            state.save()

        # ─── 0. EXIT_PENDING RECONCILIATION ─────────────────────────────────
        if pos_state == "EXIT_PENDING":
            last_attempt = pos.get("last_sell_attempt", 0)
            if time.time() - last_attempt >= 5.0:
                pos["last_sell_attempt"] = time.time()
                reason = pos.get("pending_exit_reason", "Pending Protective Exit")
                log.info(f"[EXIT-PENDING-RETRY] Retrying exit for {pair}: {reason}")
                success, err = execute_sell(pair, current_price, qty, reason, order_type="market")
                if not success:
                    pos["pending_exit_error"] = err
                    state.save()
            continue

        # ─── 1. TP_EVALUATING (Nonblocking Checkpoint) ─────────────────────
        if pos_state == "TP_EVALUATING":
            # Rule A: Check protective floor breach during evaluation immediately!
            exit_decision = decide_exit(
                entry_price=entry,
                peak_price=peak_price,
                current_price=current_price,
                trailing_armed=trailing_armed,
                activation_pct=TRAILING_ACTIVATION_PCT,
                trail_pct=TRAILING_STOP_PCT,
                hard_stop_pct=STOP_LOSS_PCT,
                side="LONG",
                mode="TP_EVALUATING",
                profit_floor_pct=pos.get("profit_floor_trigger_pct", pos.get("profit_floor_pct", pos.get("guaranteed_floor_pct", 0.08))),
            )

            if exit_decision.should_exit:
                log.info(f"🎯 [TP-EVAL-FLOOR-BREACH] {pair}: Protective floor breached during evaluation ({exit_decision.reason})")
                pos["state"] = "EXIT_PENDING"
                pos["pending_exit_reason"] = exit_decision.reason
                pos["last_sell_attempt"] = time.time()
                state.save()
                _active_evaluations.pop(pair, None)
                success, err = execute_sell(
                    pair, current_price, qty,
                    f"Protective Exit during TP Evaluation ({exit_decision.exit_type})",
                    order_type="market"
                )
                if not success:
                    pos["pending_exit_error"] = err
                    state.save()
                continue

            # Rule B: Check if async evaluation is completed
            eval_entry = _active_evaluations.get(pair)
            now = time.time()
            deadline = pos.get("evaluation_deadline", now + EVALUATION_DEADLINE_SECONDS)

            if eval_entry and eval_entry.get("future") and eval_entry["future"].done():
                try:
                    evaluation = eval_entry["future"].result()
                except Exception as exc:
                    log.warning(f"[TP-EVAL-ASYNC-ERROR] Async evaluation error for {pair}: {exc}")
                    evaluation = {"action": "TAKE_PROFIT_NOW", "reason": f"Evaluation error: {exc}"}

                _active_evaluations.pop(pair, None)

                # Validate position is still active and unchanged
                if (
                    pair in state.positions
                    and pos.get("state") == "TP_EVALUATING"
                    and pos.get("position_lifecycle_id") == eval_entry.get("position_lifecycle_id")
                    and pos.get("position_version") == eval_entry.get("position_version")
                ):
                    if evaluation.get("action") == "EXTEND_AND_RIDE":
                        pos["state"] = "RIDING"
                        pos["mode"] = "RIDING_TREND"
                        floor_pct_val = float(evaluation.get("profit_floor_trigger_pct", evaluation.get("guaranteed_floor_pct", 0.08)))
                        trail_pct_val = float(evaluation.get("recommended_trail_pct", 0.035))
                        pos["profit_floor_pct"] = floor_pct_val
                        pos["profit_floor_trigger_pct"] = floor_pct_val
                        pos["guaranteed_floor_pct"] = floor_pct_val
                        pos["trailing_stop_pct"] = trail_pct_val
                        pos["evaluation_reason"] = str(evaluation.get("reason", ""))
                        state.save()

                        floor_price = entry * (1.0 + floor_pct_val)
                        log.info(
                            f"🚀 [TP-EXTENDED] {pair}: Trend Extension activated! "
                            f"Floor Trigger: +{floor_pct_val*100:.1f}% ({format_price(floor_price)})"
                        )
                        try:
                            send_tp_extension_alert(
                                pair=pair,
                                current_price=current_price,
                                entry_price=entry,
                                pnl_pct=pnl_pct,
                                floor_price=floor_price,
                                floor_pct=floor_pct_val,
                                trail_pct=trail_pct_val,
                                reason=str(evaluation.get("reason", "")),
                                sentiment_str=str(evaluation.get("sentiment_summary", "")),
                                is_futures=False,
                                side="LONG",
                                source=str(evaluation.get("source", "DETERMINISTIC")),
                            )
                        except Exception as te:
                            log.debug(f"Extension alert error: {te}")
                        continue
                    else:
                        reason_text = evaluation.get("reason", "Take profit secured at +10%")
                        log.info(f"🎯 [TP-EXIT] {pair}: Taking immediate profit at +{pnl_pct*100:.1f}% ({reason_text})")
                        pos["state"] = "EXIT_PENDING"
                        pos["pending_exit_reason"] = reason_text
                        pos["last_sell_attempt"] = time.time()
                        state.save()
                        success, err = execute_sell(
                            pair, current_price, qty,
                            f"Take Profit (+{pnl_pct*100:.1f}%)",
                            order_type="market"
                        )
                        if not success:
                            pos["pending_exit_error"] = err
                            state.save()
                        continue
                else:
                    log.warning(f"[TP-EVAL-STALE] Discarding late AI response for {pair} (position state or version changed)")
                    continue

            elif now >= deadline:
                log.warning(f"🎯 [TP-DEADLINE-EXPIRED] {pair} evaluation exceeded 3s deadline. Failing closed to Take Profit Exit.")
                pos["state"] = "EXIT_PENDING"
                pos["pending_exit_reason"] = "Evaluation Deadline Expired"
                pos["last_sell_attempt"] = time.time()
                state.save()
                _active_evaluations.pop(pair, None)
                success, err = execute_sell(
                    pair, current_price, qty,
                    f"Take Profit (+{pnl_pct*100:.1f}% - Deadline Expired)",
                    order_type="market"
                )
                if not success:
                    pos["pending_exit_error"] = err
                    state.save()
                continue

            else:
                # Evaluation in flight within deadline; continue without blocking the loop
                continue

        # ─── 2. DYNAMIC RIDING TREND MODE ──────────────────────────────────
        if pos_state == "RIDING" or pos.get("mode") == "RIDING_TREND":
            exit_decision = decide_exit(
                entry_price=entry,
                peak_price=peak_price,
                current_price=current_price,
                trailing_armed=trailing_armed,
                activation_pct=TRAILING_ACTIVATION_PCT,
                trail_pct=pos.get("trailing_stop_pct", 0.035),
                hard_stop_pct=STOP_LOSS_PCT,
                side="LONG",
                mode="RIDING",
                profit_floor_pct=pos.get("profit_floor_trigger_pct", pos.get("profit_floor_pct", pos.get("guaranteed_floor_pct", 0.08))),
                riding_trail_pct=pos.get("trailing_stop_pct", 0.035),
            )

            # Update ratcheted floor in state
            if exit_decision.profit_floor_price is not None:
                updated_floor_pct = float((exit_decision.profit_floor_price - Decimal(str(entry))) / Decimal(str(entry)))
                current_floor = pos.get("profit_floor_trigger_pct", pos.get("profit_floor_pct", pos.get("guaranteed_floor_pct", 0.08)))
                if updated_floor_pct > current_floor:
                    pos["profit_floor_pct"] = updated_floor_pct
                    pos["profit_floor_trigger_pct"] = updated_floor_pct
                    pos["guaranteed_floor_pct"] = updated_floor_pct
                    log.info(f"🚀 [FLOOR-RATCHET] {pair}: Profit floor trigger raised to +{updated_floor_pct*100:.1f}%")
                    state.save()

            if exit_decision.should_exit:
                peak_pnl_pct = (peak_price - entry) / entry
                log.info(f"🎯 [DYNAMIC-TP-EXIT] {pair}: Closed at +{pnl_pct*100:.1f}% (Peak: +{peak_pnl_pct*100:.1f}%)")
                pos["state"] = "EXIT_PENDING"
                pos["pending_exit_reason"] = exit_decision.reason
                pos["last_sell_attempt"] = time.time()
                state.save()
                success, err = execute_sell(
                    pair, current_price, qty,
                    f"Dynamic TP Exit (Peak +{peak_pnl_pct*100:.1f}%)",
                    order_type="market"
                )
                if not success:
                    pos["pending_exit_error"] = err
                    state.save()
                continue
            else:
                continue

        # ─── 3. PRIMARY TAKE PROFIT CHECKPOINT (+10.0%) ─────────────────────
        if pnl_pct >= TAKE_PROFIT_PCT and not pos.get("tp_evaluated", False):
            pos["tp_evaluated"] = True
            pos["state"] = "TP_EVALUATING"
            pos["evaluation_started_at"] = time.time()
            pos["evaluation_deadline"] = time.time() + EVALUATION_DEADLINE_SECONDS
            pos["position_lifecycle_id"] = lifecycle_id
            pos["position_version"] = version
            state.save()

            log.info(f"🎯 [TP-CHECKPOINT] {pair} reached +{pnl_pct*100:.1f}%! Spawning Nonblocking Momentum Evaluation...")

            future = _eval_executor.submit(
                evaluate_tp_momentum,
                pair=pair,
                current_price=current_price,
                entry_price=entry,
                pnl_pct=pnl_pct,
                is_futures=False,
                side="LONG",
                position_lifecycle_id=lifecycle_id,
                position_version=version,
            )
            _active_evaluations[pair] = {
                "future": future,
                "started_at": pos["evaluation_started_at"],
                "deadline": pos["evaluation_deadline"],
                "position_lifecycle_id": lifecycle_id,
                "position_version": version,
            }
            continue

        # ─── 4. STANDARD & TRAILING STOP EXITS ──────────────────────────────
        exit_decision = decide_exit(
            entry_price=entry,
            peak_price=peak_price,
            current_price=current_price,
            trailing_armed=trailing_armed,
            activation_pct=TRAILING_ACTIVATION_PCT,
            trail_pct=TRAILING_STOP_PCT,
            hard_stop_pct=STOP_LOSS_PCT,
            side="LONG",
            mode=pos_state,
        )

        if exit_decision.should_exit:
            log.info(f"🎯 [{exit_decision.exit_type}] {pair}: {exit_decision.reason}")
            pos["state"] = "EXIT_PENDING"
            pos["pending_exit_reason"] = exit_decision.reason
            pos["last_sell_attempt"] = time.time()
            state.save()
            success, err = execute_sell(
                pair, current_price, qty,
                exit_decision.exit_type.replace("_", " ").title(),
                order_type="market"
            )
            if not success:
                pos["pending_exit_error"] = err
                state.save()
            continue

        # ─── 5. SIGNAL-BASED EARLY EXIT (Confirmed reversal policy) ─────────
        # Evaluated only if profit is >= +2.0% and not in riding/evaluating
        if pnl_pct >= 0.02 and pos_state not in ("RIDING", "TP_EVALUATING", "EXIT_PENDING"):
            cached_mrsi = _multi_rsi_cache.get(pair, {})
            if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
                multi_rsi = dict(cached_mrsi["rsi"])
                multi_rsi["3m"] = get_rsi(pair)
            else:
                multi_rsi = {"3m": get_rsi(pair), "1h": 50.0, "4h": 50.0}
            signal, score, reasons = get_signal(pair, current_price, multi_rsi)

            from hermes.trading.signal_policy import evaluate_signal_precedence
            precedence_dec = evaluate_signal_precedence(
                symbol=pair,
                side="LONG",
                entry_price=entry,
                current_price=current_price,
                position_state=pos_state,
                raw_signal=signal,
                signal_score=score,
            )

            if precedence_dec.action in ("EXIT_CONFIRMED_REVERSAL", "EXIT_PROTECTIVE", "EXIT_EMERGENCY"):
                coin = pair.replace("USDT", "")
                balances = get_balance(use_cache=False)
                coin_lower = coin.lower()
                balance = balances.get(coin_lower, 0)
                if balance == 0:
                    log.warning(f"Stale position {pair} — Binance balance is 0, skipping sell and removing from state")
                    del state.positions[pair]
                    state.save()
                    continue
                log.info(f"Signal exit for {pair}: {precedence_dec.reason} with +{pnl_pct*100:.2f}%")
                pos["state"] = "EXIT_PENDING"
                pos["pending_exit_reason"] = precedence_dec.reason
                pos["last_sell_attempt"] = time.time()
                state.save()
                success, err = execute_sell(pair, current_price, qty, precedence_dec.reason)
                if not success:
                    pos["pending_exit_error"] = err
                    state.save()


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
