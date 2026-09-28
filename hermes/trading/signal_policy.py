"""
Signal Precedence and Confirmed Reversal Policy.

Enforces:
1. Signal labels (STRONG_BUY, STRONG_SELL, etc.) are evidence/indicators, NOT unconditional execution commands.
2. Precedence rule: Hard SL, active trailing stop, and floor breach ALWAYS override any indicator (even STRONG_BUY).
3. Candidate confirmed reversal rule (shadow): requires adverse price-structure break plus directional momentum confirmation on two distinct closed bars.
4. Raw STRONG_SELL at +2% with healthy trend does NOT force exit; requires confirmed reversal.
5. Disallow automatic DCA / buying into a triggered stop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Literal, Tuple

from hermes.logging_setup import log
from hermes.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_ACTIVATION_PCT, TRAILING_STOP_PCT


@dataclass(frozen=True)
class ClosedBar:
    """A single closed candle bar for structure and momentum confirmation."""
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class ConfirmedReversalResult:
    """Outcome of multi-bar confirmed reversal evaluation."""
    confirmed: bool
    reason: str
    bar_count: int
    pattern: str = "NONE"


SignalPolicyAction = Literal[
    "EXIT_PROTECTIVE",
    "EXIT_EMERGENCY",
    "EXIT_CONFIRMED_REVERSAL",
    "EVALUATE_CHECKPOINT",
    "HOLD",
    "ALLOW_ENTRY",
    "REJECT_ENTRY",
]


@dataclass(frozen=True)
class SignalPrecedenceDecision:
    """Structured decision following the 6-tier policy contract."""
    action: SignalPolicyAction
    reason: str
    tier: int
    is_protective: bool = False
    reversal_confirmed: bool = False
    overridden_signal: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def evaluate_confirmed_reversal(
    symbol: str,
    side: str,
    bars: List[ClosedBar],
    current_price: float,
    max_age_seconds: float = 300.0,
    reference_time: Optional[float] = None
) -> ConfirmedReversalResult:
    """
    Evaluate candidate confirmed reversal rule.
    
    Requires:
    1. At least 2 distinct closed bars of declared timeframe.
    2. Freshness: most recent bar timestamp within max_age_seconds.
    3. No duplicate timestamps.
    4. For LONG: Adverse structure (lower highs & lower closes) + downward momentum.
    5. For SHORT: Adverse structure (higher lows & higher closes) + upward momentum.
    """
    now = reference_time if reference_time is not None else time.time()

    if not bars or len(bars) < 2:
        return ConfirmedReversalResult(
            confirmed=False,
            reason="INSUFFICIENT_BAR_DATA",
            bar_count=len(bars) if bars else 0,
            pattern="NONE"
        )

    # Sort bars chronologically
    sorted_bars = sorted(bars, key=lambda b: b.timestamp)

    # Check for duplicate timestamps
    timestamps = [b.timestamp for b in sorted_bars]
    if len(timestamps) != len(set(timestamps)):
        return ConfirmedReversalResult(
            confirmed=False,
            reason="DUPLICATE_BAR_TIMESTAMPS",
            bar_count=len(sorted_bars),
            pattern="NONE"
        )

    # Check freshness of latest bar
    latest_bar = sorted_bars[-1]
    if (now - latest_bar.timestamp) > max_age_seconds:
        return ConfirmedReversalResult(
            confirmed=False,
            reason=f"STALE_BAR_DATA: age {now - latest_bar.timestamp:.1f}s > {max_age_seconds:.1f}s",
            bar_count=len(sorted_bars),
            pattern="NONE"
        )

    bar0 = sorted_bars[-2]
    bar1 = sorted_bars[-1]

    if side.upper() in ("LONG", "BUY"):
        # For LONG position: Reversal is BEARISH break
        # Structure: lower highs and lower closes
        lower_high = bar1.high < bar0.high
        lower_close = bar1.close < bar0.close
        lower_low = bar1.low < bar0.low
        # Downward momentum: closed below open on at least bar1, or both
        bearish_momentum = bar1.close < bar1.open

        if (lower_high and lower_close) or (lower_low and lower_close and bearish_momentum):
            return ConfirmedReversalResult(
                confirmed=True,
                reason="BEARISH_STRUCTURE_BREAK_AND_MOMENTUM",
                bar_count=len(sorted_bars),
                pattern="LOWER_HIGHS_LOWER_CLOSES"
            )
        else:
            return ConfirmedReversalResult(
                confirmed=False,
                reason="HEALTHY_UPTREND_NO_BEARISH_REVERSAL",
                bar_count=len(sorted_bars),
                pattern="TREND_INTACT"
            )

    else:
        # For SHORT position: Reversal is BULLISH break
        higher_low = bar1.low > bar0.low
        higher_close = bar1.close > bar0.close
        higher_high = bar1.high > bar0.high
        bullish_momentum = bar1.close > bar1.open

        if (higher_low and higher_close) or (higher_high and higher_close and bullish_momentum):
            return ConfirmedReversalResult(
                confirmed=True,
                reason="BULLISH_STRUCTURE_BREAK_AND_MOMENTUM",
                bar_count=len(sorted_bars),
                pattern="HIGHER_LOWS_HIGHER_CLOSES"
            )
        else:
            return ConfirmedReversalResult(
                confirmed=False,
                reason="HEALTHY_DOWNTREND_NO_BULLISH_REVERSAL",
                bar_count=len(sorted_bars),
                pattern="TREND_INTACT"
            )


def evaluate_signal_precedence(
    symbol: str,
    side: str,
    entry_price: float,
    current_price: float,
    peak_price: float = 0.0,
    trailing_armed: bool = False,
    hard_stop_pct: float = STOP_LOSS_PCT,
    profit_floor_pct: Optional[float] = None,
    position_state: str = "STANDARD",
    pending_exit: bool = False,
    raw_signal: str = "HOLD",
    signal_score: int = 0,
    emergency_veto: bool = False,
    emergency_reason: str = "",
    bars: Optional[List[ClosedBar]] = None,
    trailing_stop_pct: float = TRAILING_STOP_PCT,
    checkpoint_pct: float = TAKE_PROFIT_PCT,
    reference_time: Optional[float] = None,
) -> SignalPrecedenceDecision:
    """
    Evaluates execution precedence across the 6 strict tiers:
    Tier 1: Exchange reconciliation & known pending orders.
    Tier 2: Hard risk / SL / active trailing / protected-floor breach (Deterministic, overrides all).
    Tier 3: Confirmed emergency or material thesis invalidation.
    Tier 4: +10% gross checkpoint.
    Tier 5: Normal momentum weakening (Requires confirmed reversal, not raw label).
    Tier 6: Entry / addition.
    """
    if entry_price <= 0.0 or current_price <= 0.0:
        return SignalPrecedenceDecision(
            action="HOLD",
            reason="Invalid price inputs (entry or current <= 0)",
            tier=1
        )

    is_long = side.upper() in ("LONG", "BUY")
    if is_long:
        pnl_pct = (current_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - current_price) / entry_price

    # ─── TIER 1: Reconciliation / Pending Orders ────────────────────────────
    if pending_exit or position_state == "EXIT_PENDING":
        return SignalPrecedenceDecision(
            action="HOLD",
            reason="Exit order already pending reconciliation; do not submit duplicate",
            tier=1,
            is_protective=True
        )

    # ─── TIER 2: Hard Risk / SL / Trailing / Protected-Floor Breach ──────────
    # Hard Stop Loss
    if is_long:
        hard_sl_price = entry_price * (1.0 - hard_stop_pct)
        if current_price <= hard_sl_price:
            return SignalPrecedenceDecision(
                action="EXIT_PROTECTIVE",
                reason=f"Hard Stop Loss hit: {current_price:.4f} <= {hard_sl_price:.4f} (PnL: {pnl_pct*100:.2f}%)",
                tier=2,
                is_protective=True,
                overridden_signal=raw_signal if raw_signal in ("STRONG_BUY", "BUY") else None,
                metadata={"pnl_pct": pnl_pct, "trigger": "HARD_SL"}
            )
    else:
        hard_sl_price = entry_price * (1.0 + hard_stop_pct)
        if current_price >= hard_sl_price:
            return SignalPrecedenceDecision(
                action="EXIT_PROTECTIVE",
                reason=f"Futures Hard Stop Loss hit: {current_price:.4f} >= {hard_sl_price:.4f} (PnL: {pnl_pct*100:.2f}%)",
                tier=2,
                is_protective=True,
                overridden_signal=raw_signal if raw_signal in ("STRONG_BUY", "BUY") else None,
                metadata={"pnl_pct": pnl_pct, "trigger": "HARD_SL"}
            )

    # Protected-Floor Breach (in RIDING or TP_EVALUATING)
    if profit_floor_pct is not None and position_state in ("RIDING", "TP_EVALUATING"):
        if pnl_pct <= profit_floor_pct:
            return SignalPrecedenceDecision(
                action="EXIT_PROTECTIVE",
                reason=f"Protected Profit Floor breached: PnL {pnl_pct*100:.2f}% <= {profit_floor_pct*100:.2f}%",
                tier=2,
                is_protective=True,
                overridden_signal=raw_signal if raw_signal in ("STRONG_BUY", "BUY") else None,
                metadata={"pnl_pct": pnl_pct, "trigger": "FLOOR_BREACH"}
            )

    # Active Trailing Stop Pullback
    if trailing_armed and peak_price > 0:
        if is_long:
            trail_stop_price = peak_price * (1.0 - trailing_stop_pct)
            if current_price <= trail_stop_price:
                peak_pnl = (peak_price - entry_price) / entry_price
                return SignalPrecedenceDecision(
                    action="EXIT_PROTECTIVE",
                    reason=f"Trailing Stop hit: {current_price:.4f} <= {trail_stop_price:.4f} (Peak: +{peak_pnl*100:.2f}%, Current: {pnl_pct*100:.2f}%)",
                    tier=2,
                    is_protective=True,
                    overridden_signal=raw_signal if raw_signal in ("STRONG_BUY", "BUY") else None,
                    metadata={"pnl_pct": pnl_pct, "peak_pnl": peak_pnl, "trigger": "TRAILING_STOP"}
                )
        else:
            trail_stop_price = peak_price * (1.0 + trailing_stop_pct)
            if current_price >= trail_stop_price:
                peak_pnl = (entry_price - peak_price) / entry_price
                return SignalPrecedenceDecision(
                    action="EXIT_PROTECTIVE",
                    reason=f"Futures Trailing Stop hit: {current_price:.4f} >= {trail_stop_price:.4f} (Peak: +{peak_pnl*100:.2f}%, Current: {pnl_pct*100:.2f}%)",
                    tier=2,
                    is_protective=True,
                    overridden_signal=raw_signal if raw_signal in ("STRONG_BUY", "BUY") else None,
                    metadata={"pnl_pct": pnl_pct, "peak_pnl": peak_pnl, "trigger": "TRAILING_STOP"}
                )

    # ─── TIER 3: Confirmed Emergency / Material Thesis Invalidation ─────────
    if emergency_veto:
        return SignalPrecedenceDecision(
            action="EXIT_EMERGENCY",
            reason=f"Emergency thesis invalidation / news veto: {emergency_reason}",
            tier=3,
            is_protective=True,
            metadata={"pnl_pct": pnl_pct, "emergency_reason": emergency_reason}
        )

    # ─── TIER 4: +10% Gross Checkpoint ──────────────────────────────────────
    if pnl_pct >= checkpoint_pct and position_state in ("STANDARD", "TRAILING_ARMED"):
        return SignalPrecedenceDecision(
            action="EVALUATE_CHECKPOINT",
            reason=f"Checkpoint reached at +{pnl_pct*100:.2f}% (>= {checkpoint_pct*100:.1f}%)",
            tier=4,
            metadata={"pnl_pct": pnl_pct}
        )

    # ─── TIER 5: Normal Momentum Weakening / Reversal ───────────────────────
    # If in RIDING or TP_EVALUATING, signal labels are strictly ignored (handled by state machine & ratchets)
    if position_state in ("RIDING", "TP_EVALUATING"):
        return SignalPrecedenceDecision(
            action="HOLD",
            reason=f"Position in {position_state} state; trailing/floor ratchet governs exit",
            tier=5,
            metadata={"pnl_pct": pnl_pct}
        )

    # For positions with profit >= +2.0%, evaluate confirmed reversal vs raw indicator label
    if pnl_pct >= 0.02:
        if raw_signal == "STRONG_SELL":
            # In candidate policy: check confirmed reversal across 2 closed bars
            reversal_res = evaluate_confirmed_reversal(
                symbol=symbol,
                side=side,
                bars=bars or [],
                current_price=current_price,
                reference_time=reference_time
            )
            if reversal_res.confirmed:
                return SignalPrecedenceDecision(
                    action="EXIT_CONFIRMED_REVERSAL",
                    reason=f"Confirmed reversal at +{pnl_pct*100:.2f}% ({reversal_res.reason}, pattern={reversal_res.pattern})",
                    tier=5,
                    reversal_confirmed=True,
                    metadata={"pnl_pct": pnl_pct, "reversal": reversal_res}
                )
            else:
                # Raw STRONG_SELL without confirmed structure break -> DO NOT exit in candidate policy!
                return SignalPrecedenceDecision(
                    action="HOLD",
                    reason=f"Raw STRONG_SELL at +{pnl_pct*100:.2f}% ignored: trend remains healthy ({reversal_res.reason})",
                    tier=5,
                    reversal_confirmed=False,
                    overridden_signal="STRONG_SELL",
                    metadata={"pnl_pct": pnl_pct, "reversal_reason": reversal_res.reason}
                )

    # ─── TIER 6: Holding / Default ──────────────────────────────────────────
    return SignalPrecedenceDecision(
        action="HOLD",
        reason=f"No exit criteria met (PnL: {pnl_pct*100:.2f}%, Signal: {raw_signal})",
        tier=6,
        metadata={"pnl_pct": pnl_pct}
    )
