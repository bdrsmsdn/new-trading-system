"""
Active Capital Rotation & Opportunity Cost Module.

Enables Hermes to proactively liquidate sluggish or dead-money positions
to immediately fund high-conviction breakout opportunities (Strong Buy / High Confidence).
Prevents portfolio paralysis while strictly protecting winners and managing risk.
"""

import time
from typing import Dict, Any, Optional, Tuple

from hermes.config import (
    ROTATION_ENABLED,
    ROTATION_MIN_HOLD_SECS,
    ROTATION_SCORE_DELTA,
    ROTATION_MAX_PNL_PCT,
    ROTATION_MIN_PNL_PCT,
    ROTATION_COOLDOWN_SECS,
    MIN_TRADE_USDT,
)
from hermes.logging_setup import log
from hermes.state import state, prices
from hermes.trading.execution import execute_sell, execute_buy
from hermes.indicators.rsi import get_rsi
from hermes.indicators.signals import get_signal

_last_rotation_time: float = 0.0


def can_rotate_now() -> bool:
    """Check if capital rotation is enabled and not on cooldown."""
    if not ROTATION_ENABLED:
        return False
    global _last_rotation_time
    elapsed = time.time() - _last_rotation_time
    if elapsed < ROTATION_COOLDOWN_SECS:
        log.debug(f"[ROTATION] On cooldown ({elapsed:.0f}s / {ROTATION_COOLDOWN_SECS}s)")
        return False
    return True


def get_position_momentum_score(pair: str, current_price: float) -> Tuple[int, str]:
    """Calculate the current momentum score and signal for an open position."""
    try:
        rsi_val = get_rsi(pair)
        multi_rsi = {"3m": rsi_val, "1h": 50.0, "4h": 50.0}
        signal, score, _ = get_signal(pair, current_price, multi_rsi)
        return int(score), str(signal)
    except Exception as e:
        log.debug(f"[ROTATION] Error scoring momentum for {pair}: {e}")
        return 3, "NEUTRAL"


def evaluate_sluggish_positions(candidate_pair: str, candidate_score: int) -> Optional[Dict[str, Any]]:
    """Scan open positions and find the most suitable sluggish position to rotate out."""
    if not state.positions:
        return None

    now = time.time()
    candidates = []

    for pair, pos in list(state.positions.items()):
        if pair.upper() == candidate_pair.upper():
            continue

        entry = pos.get("entry_price", 0.0)
        qty = pos.get("qty", 0.0)
        open_time = pos.get("time", 0.0)
        mode = pos.get("mode", "")

        # 1. Never cut winners or positions actively riding trend!
        if mode == "RIDING_TREND":
            continue

        curr_price = prices.get(pair.upper(), {}).get("price", entry)
        if curr_price <= 0 or entry <= 0:
            continue

        pnl_pct = (curr_price - entry) / entry
        holding_time = now - open_time

        # 2. Protect running winners (>= +2.0%)
        if pnl_pct > ROTATION_MAX_PNL_PCT:
            continue

        # 3. Protect positions beyond hard stop-loss zone (let SL handle)
        if pnl_pct < ROTATION_MIN_PNL_PCT:
            continue

        # 4. Require minimum holding time so we don't churn fresh trades
        if holding_time < ROTATION_MIN_HOLD_SECS:
            continue

        # 5. Evaluate current momentum of the position
        pos_score, pos_signal = get_position_momentum_score(pair, curr_price)

        # 6. Check Conviction Delta (Candidate must be significantly superior)
        score_delta = candidate_score - pos_score
        if score_delta < ROTATION_SCORE_DELTA:
            continue

        value_usdt = qty * curr_price
        # Must be large enough to be tradable on Binance (>= $5.50)
        if value_usdt < MIN_TRADE_USDT:
            continue

        candidates.append({
            "pair": pair,
            "entry_price": entry,
            "current_price": curr_price,
            "qty": qty,
            "value_usdt": value_usdt,
            "pnl_pct": pnl_pct,
            "holding_hours": holding_time / 3600.0,
            "pos_score": pos_score,
            "pos_signal": pos_signal,
            "score_delta": score_delta,
        })

    if not candidates:
        return None

    # Priority:
    # 1. Highest score delta (most sluggish vs incoming candidate)
    # 2. Highest value_usdt (frees the most trapped capital, e.g. BTC)
    candidates.sort(key=lambda x: (x["score_delta"], x["value_usdt"]), reverse=True)
    return candidates[0]


def execute_capital_rotation(
    candidate_pair: str,
    candidate_price: float,
    candidate_score: int,
    candidate_confidence: str,
    candidate_signal_type: str = "LONG",
    get_balance_func = None
) -> Tuple[bool, str]:
    """Execute capital rotation: liquidate the chosen sluggish position and enter the candidate."""
    global _last_rotation_time

    if not can_rotate_now():
        return False, "Rotation disabled or in cooldown"

    target_pos = evaluate_sluggish_positions(candidate_pair, candidate_score)
    if not target_pos:
        return False, "No sluggish position qualifies for rotation"

    stagnant_pair = target_pos["pair"]
    stagnant_price = target_pos["current_price"]
    stagnant_qty = target_pos["qty"]
    stagnant_pnl = target_pos["pnl_pct"]
    stagnant_score = target_pos["pos_score"]
    freed_est = target_pos["value_usdt"]

    log.info(
        f"[ROTATION] 🔄 Initiating Capital Rotation: Liquidating sluggish {stagnant_pair} "
        f"(PnL: {stagnant_pnl*100:+.1f}%, Score: {stagnant_score}/10, Value: ${freed_est:.2f}) "
        f"-> Reallocating to breakout candidate {candidate_pair} (Score: {candidate_score}/10, {candidate_confidence})"
    )

    # Step 1: Liquidate sluggish position
    sell_success, sell_msg = execute_sell(
        stagnant_pair,
        stagnant_price,
        stagnant_qty,
        reason=f"Capital Rotation -> {candidate_pair}",
        order_type="market"
    )

    if not sell_success:
        log.error(f"[ROTATION] ❌ Failed to liquidate {stagnant_pair}: {sell_msg}. Aborting rotation.")
        return False, f"Failed to liquidate {stagnant_pair}: {sell_msg}"

    # Step 2: Fetch fresh USDT balance
    if get_balance_func:
        bal = get_balance_func(use_cache=False)
        fresh_usdt = bal.get("usdt", 0.0)
    else:
        from hermes.api.balance import get_balance
        bal = get_balance(use_cache=False)
        fresh_usdt = bal.get("usdt", 0.0)

    if fresh_usdt < MIN_TRADE_USDT:
        log.error(f"[ROTATION] ❌ Insufficient USDT after liquidating {stagnant_pair}: ${fresh_usdt:.2f}")
        return False, f"Insufficient USDT after liquidation: ${fresh_usdt:.2f}"

    # Step 3: Execute BUY on high-conviction candidate
    buy_success, buy_msg = execute_buy(
        candidate_pair,
        candidate_price,
        fresh_usdt,
        confidence=candidate_confidence,
        score=candidate_score
    )

    if buy_success:
        _last_rotation_time = time.time()
        log.info(f"[ROTATION] ✅ Capital Rotation successful! Rotated {stagnant_pair} -> {candidate_pair}")

        # Step 4: Dispatch Telegram alert
        try:
            from hermes.notifications.telegram import telegram_rotation_alert
            telegram_rotation_alert(
                liquidated_pair=stagnant_pair,
                liquidated_pnl=stagnant_pnl,
                liquidated_score=stagnant_score,
                freed_usdt=freed_est,
                new_pair=candidate_pair,
                new_price=candidate_price,
                new_score=candidate_score,
                new_confidence=candidate_confidence,
                new_signal_type=candidate_signal_type
            )
        except Exception as te:
            log.error(f"[ROTATION] Telegram alert failed: {te}")

        return True, f"Successfully rotated {stagnant_pair} into {candidate_pair}"
    else:
        log.error(f"[ROTATION] ⚠️ Sold {stagnant_pair} but buy for {candidate_pair} failed: {buy_msg}")
        return False, f"Sold {stagnant_pair} but failed to buy {candidate_pair}: {buy_msg}"
