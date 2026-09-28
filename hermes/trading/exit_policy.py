"""
Pure Exit Policy & Monotonic Protection Engine for Spot & Futures.

Normative implementation adhering to docs/trading-risk-contract.md:
- Pure deterministic exit evaluation with Decimal arithmetic.
- Trailing stop arming invariant: armed stop survives retracements (e.g. 100 -> 106 -> 103).
- Monotonic stop tightening: stops can only tighten, never loosen or widen.
- Directional symmetry: handles both LONG and SHORT positions.
- Strict unit separation: spot price fraction (spot_price_trail_fraction) vs futures ROE points (futures_roe_drawdown_points).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Literal, Optional, Union

PositionSide = Literal["LONG", "SHORT"]
ExitType = Literal[
    "NONE",
    "STOP_LOSS",
    "TRAILING_STOP",
    "PROFIT_FLOOR",
    "TAKE_PROFIT_CHECKPOINT",
    "EVALUATION_PROTECTIVE_EXIT",
    "EMERGENCY",
]

def _to_decimal(val: Union[Decimal, float, int, str, None], default: str = "0") -> Decimal:
    """Convert value safely to Decimal, avoiding binary float artifacts."""
    if val is None:
        return Decimal(default)
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


@dataclass(frozen=True)
class ExitDecision:
    """Immutable exit decision for Spot trading."""
    should_exit: bool
    exit_type: ExitType
    reason: str
    trailing_armed: bool
    effective_stop_price: Decimal
    hard_stop_price: Decimal
    trailing_stop_price: Optional[Decimal]
    profit_floor_price: Optional[Decimal]
    pnl_fraction: Decimal
    peak_pnl_fraction: Decimal
    peak_price: Decimal
    side: PositionSide

    def to_dict(self) -> Dict[str, Any]:
        return {
            "should_exit": self.should_exit,
            "exit_type": self.exit_type,
            "reason": self.reason,
            "trailing_armed": self.trailing_armed,
            "effective_stop_price": str(self.effective_stop_price),
            "hard_stop_price": str(self.hard_stop_price),
            "trailing_stop_price": str(self.trailing_stop_price) if self.trailing_stop_price is not None else None,
            "profit_floor_price": str(self.profit_floor_price) if self.profit_floor_price is not None else None,
            "pnl_fraction": str(self.pnl_fraction),
            "peak_pnl_fraction": str(self.peak_pnl_fraction),
            "peak_price": str(self.peak_price),
            "side": self.side,
        }


@dataclass(frozen=True)
class FuturesExitDecision:
    """Immutable exit decision for Futures trading (ROE percentage-point units)."""
    should_exit: bool
    exit_type: ExitType
    reason: str
    trailing_armed: bool
    effective_floor_roe: Decimal
    hard_stop_roe: Decimal
    trail_trigger_roe: Optional[Decimal]
    profit_floor_roe: Optional[Decimal]
    current_roe: Decimal
    peak_roe: Decimal
    side: PositionSide

    def to_dict(self) -> Dict[str, Any]:
        return {
            "should_exit": self.should_exit,
            "exit_type": self.exit_type,
            "reason": self.reason,
            "trailing_armed": self.trailing_armed,
            "effective_floor_roe": str(self.effective_floor_roe),
            "hard_stop_roe": str(self.hard_stop_roe),
            "trail_trigger_roe": str(self.trail_trigger_roe) if self.trail_trigger_roe is not None else None,
            "profit_floor_roe": str(self.profit_floor_roe) if self.profit_floor_roe is not None else None,
            "current_roe": str(self.current_roe),
            "peak_roe": str(self.peak_roe),
            "side": self.side,
        }


def decide_exit(
    entry_price: Union[Decimal, float, str],
    peak_price: Union[Decimal, float, str],
    current_price: Union[Decimal, float, str],
    trailing_armed: bool = False,
    activation_pct: Union[Decimal, float, str] = Decimal("0.06"),
    trail_pct: Union[Decimal, float, str] = Decimal("0.025"),
    hard_stop_pct: Union[Decimal, float, str] = Decimal("0.05"),
    side: str = "LONG",
    mode: str = "STANDARD",
    profit_floor_pct: Optional[Union[Decimal, float, str]] = None,
    riding_trail_pct: Optional[Union[Decimal, float, str]] = None,
    current_stop: Optional[Union[Decimal, float, str]] = None,
) -> ExitDecision:
    """
    Pure deterministic exit policy calculation for Spot positions.

    Invariants:
    - Trailing stop arming survives price retracements.
    - Stop prices tighten monotonically (max for LONG, min for SHORT).
    - Hard SL and trailing stops are enforced deterministically.
    """
    entry = _to_decimal(entry_price)
    peak = _to_decimal(peak_price)
    current = _to_decimal(current_price)
    activation = _to_decimal(activation_pct)
    trail = _to_decimal(trail_pct)
    hard_stop = _to_decimal(hard_stop_pct)
    side_norm: PositionSide = "SHORT" if str(side).upper() == "SHORT" else "LONG"
    mode_norm = str(mode).upper()

    if entry <= Decimal("0"):
        return ExitDecision(
            should_exit=False,
            exit_type="NONE",
            reason="Invalid entry price <= 0",
            trailing_armed=False,
            effective_stop_price=Decimal("0"),
            hard_stop_price=Decimal("0"),
            trailing_stop_price=None,
            profit_floor_price=None,
            pnl_fraction=Decimal("0"),
            peak_pnl_fraction=Decimal("0"),
            peak_price=peak,
            side=side_norm,
        )

    if side_norm == "LONG":
        effective_peak = max(peak, current, entry)
        pnl_fraction = (current - entry) / entry
        peak_pnl_fraction = (effective_peak - entry) / entry

        # Arm trailing stop if peak reached activation or already armed
        is_armed = bool(trailing_armed or (peak_pnl_fraction >= activation))

        hard_stop_price = entry * (Decimal("1") - hard_stop)
        trailing_stop_price = (effective_peak * (Decimal("1") - trail)) if is_armed else None

        # Mode-specific evaluation
        if mode_norm in ("RIDING", "RIDING_TREND"):
            floor_pct = _to_decimal(profit_floor_pct, default="0.08")
            # Auto-ratchet floor for LONG
            if peak_pnl_fraction >= Decimal("0.40") and floor_pct < Decimal("0.35"):
                floor_pct = Decimal("0.35")
            elif peak_pnl_fraction >= Decimal("0.30") and floor_pct < Decimal("0.25"):
                floor_pct = Decimal("0.25")
            elif peak_pnl_fraction >= Decimal("0.20") and floor_pct < Decimal("0.15"):
                floor_pct = Decimal("0.15")

            r_trail = _to_decimal(riding_trail_pct, default=str(trail))
            floor_price = entry * (Decimal("1") + floor_pct)
            r_trail_stop = effective_peak * (Decimal("1") - r_trail)

            effective_stop = max(hard_stop_price, floor_price, r_trail_stop)
            if current_stop is not None:
                effective_stop = max(effective_stop, _to_decimal(current_stop))

            if current <= effective_stop:
                exit_type: ExitType = "PROFIT_FLOOR" if effective_stop == floor_price else "TRAILING_STOP"
                reason = (
                    f"Dynamic TP Exit (Floor/Trail hit: current {current} <= effective_stop {effective_stop}, "
                    f"PnL: +{pnl_fraction*100:.2f}%, Peak: +{peak_pnl_fraction*100:.2f}%)"
                )
                return ExitDecision(
                    should_exit=True,
                    exit_type=exit_type,
                    reason=reason,
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=r_trail_stop,
                    profit_floor_price=floor_price,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )
            else:
                return ExitDecision(
                    should_exit=False,
                    exit_type="NONE",
                    reason="Riding trend within protected boundaries",
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=r_trail_stop,
                    profit_floor_price=floor_price,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )

        elif mode_norm == "TP_EVALUATING":
            floor_pct = _to_decimal(profit_floor_pct, default="0.08")
            floor_price = entry * (Decimal("1") + floor_pct)
            effective_stop = hard_stop_price
            if trailing_stop_price is not None:
                effective_stop = max(effective_stop, trailing_stop_price)
            # If price reached TP territory, protective floor must be held
            effective_stop = max(effective_stop, floor_price)
            if current_stop is not None:
                effective_stop = max(effective_stop, _to_decimal(current_stop))

            if current <= effective_stop:
                exit_type = "EVALUATION_PROTECTIVE_EXIT"
                reason = (
                    f"Protective Exit during TP Evaluation (Floor breach: current {current} <= {effective_stop}, "
                    f"PnL: +{pnl_fraction*100:.2f}%)"
                )
                return ExitDecision(
                    should_exit=True,
                    exit_type=exit_type,
                    reason=reason,
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=floor_price,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )
            else:
                return ExitDecision(
                    should_exit=False,
                    exit_type="NONE",
                    reason="Evaluating TP momentum under protective floor",
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=floor_price,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )

        else:
            # STANDARD / TRAILING_ARMED
            effective_stop = hard_stop_price
            if is_armed and trailing_stop_price is not None:
                effective_stop = max(effective_stop, trailing_stop_price)
            if current_stop is not None:
                effective_stop = max(effective_stop, _to_decimal(current_stop))

            if current <= hard_stop_price:
                return ExitDecision(
                    should_exit=True,
                    exit_type="STOP_LOSS",
                    reason=f"Hard Stop Loss hit: {current} <= {hard_stop_price} (PnL: {pnl_fraction*100:.2f}%)",
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=None,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )
            elif is_armed and trailing_stop_price is not None and current <= trailing_stop_price:
                return ExitDecision(
                    should_exit=True,
                    exit_type="TRAILING_STOP",
                    reason=(
                        f"Trailing Stop hit: {current} <= {trailing_stop_price} "
                        f"(Peak: {effective_peak}, PnL: {pnl_fraction*100:.2f}%)"
                    ),
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=None,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )
            else:
                return ExitDecision(
                    should_exit=False,
                    exit_type="NONE",
                    reason="Position within normal limits",
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=None,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )

    else:
        # SHORT: favorable price is lower (trough is peak)
        effective_peak = min(peak if peak > Decimal("0") else entry, current, entry)
        pnl_fraction = (entry - current) / entry
        peak_pnl_fraction = (entry - effective_peak) / entry

        is_armed = bool(trailing_armed or (peak_pnl_fraction >= activation))

        hard_stop_price = entry * (Decimal("1") + hard_stop)
        trailing_stop_price = (effective_peak * (Decimal("1") + trail)) if is_armed else None

        if mode_norm in ("RIDING", "RIDING_TREND"):
            floor_pct = _to_decimal(profit_floor_pct, default="0.08")
            if peak_pnl_fraction >= Decimal("0.40") and floor_pct < Decimal("0.35"):
                floor_pct = Decimal("0.35")
            elif peak_pnl_fraction >= Decimal("0.30") and floor_pct < Decimal("0.25"):
                floor_pct = Decimal("0.25")
            elif peak_pnl_fraction >= Decimal("0.20") and floor_pct < Decimal("0.15"):
                floor_pct = Decimal("0.15")

            r_trail = _to_decimal(riding_trail_pct, default=str(trail))
            floor_price = entry * (Decimal("1") - floor_pct)
            r_trail_stop = effective_peak * (Decimal("1") + r_trail)

            # For SHORT, tighter stop is lower price (min)
            effective_stop = min(hard_stop_price, floor_price, r_trail_stop)
            if current_stop is not None:
                effective_stop = min(effective_stop, _to_decimal(current_stop))

            if current >= effective_stop:
                exit_type = "PROFIT_FLOOR" if effective_stop == floor_price else "TRAILING_STOP"
                reason = (
                    f"Dynamic TP Exit (Floor/Trail hit: current {current} >= effective_stop {effective_stop}, "
                    f"PnL: +{pnl_fraction*100:.2f}%, Peak: +{peak_pnl_fraction*100:.2f}%)"
                )
                return ExitDecision(
                    should_exit=True,
                    exit_type=exit_type,
                    reason=reason,
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=r_trail_stop,
                    profit_floor_price=floor_price,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )
            else:
                return ExitDecision(
                    should_exit=False,
                    exit_type="NONE",
                    reason="Riding trend within protected boundaries",
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=r_trail_stop,
                    profit_floor_price=floor_price,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )

        elif mode_norm == "TP_EVALUATING":
            floor_pct = _to_decimal(profit_floor_pct, default="0.08")
            floor_price = entry * (Decimal("1") - floor_pct)
            effective_stop = hard_stop_price
            if trailing_stop_price is not None:
                effective_stop = min(effective_stop, trailing_stop_price)
            effective_stop = min(effective_stop, floor_price)
            if current_stop is not None:
                effective_stop = min(effective_stop, _to_decimal(current_stop))

            if current >= effective_stop:
                exit_type = "EVALUATION_PROTECTIVE_EXIT"
                reason = (
                    f"Protective Exit during TP Evaluation (Floor breach: current {current} >= {effective_stop}, "
                    f"PnL: +{pnl_fraction*100:.2f}%)"
                )
                return ExitDecision(
                    should_exit=True,
                    exit_type=exit_type,
                    reason=reason,
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=floor_price,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )
            else:
                return ExitDecision(
                    should_exit=False,
                    exit_type="NONE",
                    reason="Evaluating TP momentum under protective floor",
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=floor_price,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )

        else:
            effective_stop = hard_stop_price
            if is_armed and trailing_stop_price is not None:
                effective_stop = min(effective_stop, trailing_stop_price)
            if current_stop is not None:
                effective_stop = min(effective_stop, _to_decimal(current_stop))

            if current >= hard_stop_price:
                return ExitDecision(
                    should_exit=True,
                    exit_type="STOP_LOSS",
                    reason=f"Hard Stop Loss hit: {current} >= {hard_stop_price} (PnL: {pnl_fraction*100:.2f}%)",
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=None,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )
            elif is_armed and trailing_stop_price is not None and current >= trailing_stop_price:
                return ExitDecision(
                    should_exit=True,
                    exit_type="TRAILING_STOP",
                    reason=(
                        f"Trailing Stop hit: {current} >= {trailing_stop_price} "
                        f"(Peak: {effective_peak}, PnL: {pnl_fraction*100:.2f}%)"
                    ),
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=None,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )
            else:
                return ExitDecision(
                    should_exit=False,
                    exit_type="NONE",
                    reason="Position within normal limits",
                    trailing_armed=is_armed,
                    effective_stop_price=effective_stop,
                    hard_stop_price=hard_stop_price,
                    trailing_stop_price=trailing_stop_price,
                    profit_floor_price=None,
                    pnl_fraction=pnl_fraction,
                    peak_pnl_fraction=peak_pnl_fraction,
                    peak_price=effective_peak,
                    side=side_norm,
                )


def decide_futures_exit(
    peak_roe: Union[Decimal, float, str],
    current_roe: Union[Decimal, float, str],
    trailing_armed: bool = False,
    activation_roe: Union[Decimal, float, str] = Decimal("0.06"),
    futures_roe_drawdown_points: Union[Decimal, float, str] = Decimal("0.025"),
    hard_stop_roe: Union[Decimal, float, str] = Decimal("0.05"),
    side: str = "LONG",
    mode: str = "STANDARD",
    floor_trigger_roe: Optional[Union[Decimal, float, str]] = None,
    riding_roe_drawdown_points: Optional[Union[Decimal, float, str]] = None,
    current_floor_roe: Optional[Union[Decimal, float, str]] = None,
) -> FuturesExitDecision:
    """
    Pure deterministic exit calculation for Futures using ROE percentage-point units.

    ROE is directional (higher is always more profit for both LONG and SHORT).
    Drawdown is measured in ROE points (e.g. 0.025 = 2.5 ROE points).
    """
    p_roe = _to_decimal(peak_roe)
    c_roe = _to_decimal(current_roe)
    act_roe = _to_decimal(activation_roe)
    trail_roe = _to_decimal(futures_roe_drawdown_points)
    stop_roe = _to_decimal(hard_stop_roe)
    side_norm: PositionSide = "SHORT" if str(side).upper() == "SHORT" else "LONG"
    mode_norm = str(mode).upper()

    effective_peak_roe = max(p_roe, c_roe)
    is_armed = bool(trailing_armed or (effective_peak_roe >= act_roe))

    hard_stop_roe_val = -stop_roe
    trail_trigger_roe = (effective_peak_roe - trail_roe) if is_armed else None

    if mode_norm in ("RIDING", "RIDING_TREND"):
        floor_roe = _to_decimal(floor_trigger_roe, default="0.08")
        if effective_peak_roe >= Decimal("0.50") and floor_roe < Decimal("0.40"):
            floor_roe = Decimal("0.40")
        elif effective_peak_roe >= Decimal("0.30") and floor_roe < Decimal("0.25"):
            floor_roe = Decimal("0.25")
        elif effective_peak_roe >= Decimal("0.20") and floor_roe < Decimal("0.15"):
            floor_roe = Decimal("0.15")

        r_trail_roe = _to_decimal(riding_roe_drawdown_points, default=str(trail_roe))
        r_trail_trigger = effective_peak_roe - r_trail_roe

        effective_floor = max(hard_stop_roe_val, floor_roe, r_trail_trigger)
        if current_floor_roe is not None:
            effective_floor = max(effective_floor, _to_decimal(current_floor_roe))

        if c_roe <= effective_floor:
            exit_type: ExitType = "PROFIT_FLOOR" if effective_floor == floor_roe else "TRAILING_STOP"
            reason = (
                f"Dynamic Futures TP Exit (Floor/Trail hit: ROE {c_roe*100:.2f}% <= {effective_floor*100:.2f}%, "
                f"Peak ROE: +{effective_peak_roe*100:.2f}%)"
            )
            return FuturesExitDecision(
                should_exit=True,
                exit_type=exit_type,
                reason=reason,
                trailing_armed=is_armed,
                effective_floor_roe=effective_floor,
                hard_stop_roe=hard_stop_roe_val,
                trail_trigger_roe=r_trail_trigger,
                profit_floor_roe=floor_roe,
                current_roe=c_roe,
                peak_roe=effective_peak_roe,
                side=side_norm,
            )
        else:
            return FuturesExitDecision(
                should_exit=False,
                exit_type="NONE",
                reason="Riding futures trend within protected boundaries",
                trailing_armed=is_armed,
                effective_floor_roe=effective_floor,
                hard_stop_roe=hard_stop_roe_val,
                trail_trigger_roe=r_trail_trigger,
                profit_floor_roe=floor_roe,
                current_roe=c_roe,
                peak_roe=effective_peak_roe,
                side=side_norm,
            )

    elif mode_norm == "TP_EVALUATING":
        floor_roe = _to_decimal(floor_trigger_roe, default="0.08")
        effective_floor = hard_stop_roe_val
        if trail_trigger_roe is not None:
            effective_floor = max(effective_floor, trail_trigger_roe)
        effective_floor = max(effective_floor, floor_roe)
        if current_floor_roe is not None:
            effective_floor = max(effective_floor, _to_decimal(current_floor_roe))

        if c_roe <= effective_floor:
            exit_type = "EVALUATION_PROTECTIVE_EXIT"
            reason = (
                f"Protective Exit during Futures TP Evaluation (ROE breach: {c_roe*100:.2f}% <= {effective_floor*100:.2f}%)"
            )
            return FuturesExitDecision(
                should_exit=True,
                exit_type=exit_type,
                reason=reason,
                trailing_armed=is_armed,
                effective_floor_roe=effective_floor,
                hard_stop_roe=hard_stop_roe_val,
                trail_trigger_roe=trail_trigger_roe,
                profit_floor_roe=floor_roe,
                current_roe=c_roe,
                peak_roe=effective_peak_roe,
                side=side_norm,
            )
        else:
            return FuturesExitDecision(
                should_exit=False,
                exit_type="NONE",
                reason="Evaluating Futures TP momentum under protective floor",
                trailing_armed=is_armed,
                effective_floor_roe=effective_floor,
                hard_stop_roe=hard_stop_roe_val,
                trail_trigger_roe=trail_trigger_roe,
                profit_floor_roe=floor_roe,
                current_roe=c_roe,
                peak_roe=effective_peak_roe,
                side=side_norm,
            )

    else:
        # STANDARD / TRAILING_ARMED
        effective_floor = hard_stop_roe_val
        if is_armed and trail_trigger_roe is not None:
            effective_floor = max(effective_floor, trail_trigger_roe)
        if current_floor_roe is not None:
            effective_floor = max(effective_floor, _to_decimal(current_floor_roe))

        if c_roe <= hard_stop_roe_val:
            return FuturesExitDecision(
                should_exit=True,
                exit_type="STOP_LOSS",
                reason=f"Futures Hard Stop Loss hit: ROE {c_roe*100:.2f}% <= {hard_stop_roe_val*100:.2f}%",
                trailing_armed=is_armed,
                effective_floor_roe=effective_floor,
                hard_stop_roe=hard_stop_roe_val,
                trail_trigger_roe=trail_trigger_roe,
                profit_floor_roe=None,
                current_roe=c_roe,
                peak_roe=effective_peak_roe,
                side=side_norm,
            )
        elif is_armed and trail_trigger_roe is not None and c_roe <= trail_trigger_roe:
            return FuturesExitDecision(
                should_exit=True,
                exit_type="TRAILING_STOP",
                reason=(
                    f"Futures Trailing Stop hit: ROE {c_roe*100:.2f}% <= {trail_trigger_roe*100:.2f}% "
                    f"(Peak ROE: +{effective_peak_roe*100:.2f}%)"
                ),
                trailing_armed=is_armed,
                effective_floor_roe=effective_floor,
                hard_stop_roe=hard_stop_roe_val,
                trail_trigger_roe=trail_trigger_roe,
                profit_floor_roe=None,
                current_roe=c_roe,
                peak_roe=effective_peak_roe,
                side=side_norm,
            )
        else:
            return FuturesExitDecision(
                should_exit=False,
                exit_type="NONE",
                reason="Futures position within normal limits",
                trailing_armed=is_armed,
                effective_floor_roe=effective_floor,
                hard_stop_roe=hard_stop_roe_val,
                trail_trigger_roe=trail_trigger_roe,
                profit_floor_roe=None,
                current_roe=c_roe,
                peak_roe=effective_peak_roe,
                side=side_norm,
            )
