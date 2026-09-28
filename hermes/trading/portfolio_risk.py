"""
Centralized Portfolio Risk Gate and Circuit Breaker Management.

Enforces:
1. Futures entry disablement by default (fails closed unless explicitly enabled).
2. Per-trade risk budget caps (e.g., 0.5% equity).
3. Aggregate planned stop loss risk caps (e.g., 2.0% equity).
4. Daily mark-to-market loss circuit breaker halt (e.g., 2.0% equity).
5. Tradable equity USDT cash reserve protection (e.g., 25% minimum).
6. Fail-closed on stale or invalid market/balance data.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, Literal

from hermes.logging_setup import log
from hermes.config import (
    FUTURES_ENABLED,
    MIN_TRADE_USDT,
    STOP_LOSS_PCT,
    RISK_BUDGET_PER_TRADE_PCT,
    MAX_AGGREGATE_STOP_RISK_PCT,
    DAILY_LOSS_CIRCUIT_BREAKER_PCT,
    MIN_EQUITY_USDT_RESERVE_PCT,
)

RiskReasonCode = Literal[
    "ALLOW",
    "STALE_SNAPSHOT",
    "UNKNOWN_DATA",
    "INVALID_DATA",
    "POSITION_CHANGED",
    "PENDING_ORDER",
    "RECONCILIATION_REQUIRED",
    "ENTRY_DISABLED",
    "FUTURES_ENTRY_DISABLED",
    "RISK_BUDGET_EXCEEDED",
    "INSUFFICIENT_RESERVE",
    "MIN_NOTIONAL_EXCEEDS_BUDGET",
    "CIRCUIT_BREAKER_ACTIVE",
    "INVALID_STRATEGY_SETUP",
]


@dataclass(frozen=True)
class RiskDecision:
    """Immutable decision from the unified portfolio risk gate."""
    allowed: bool
    reason_code: RiskReasonCode
    snapshot_id: str
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# In-memory circuit breaker state with daily scope
_circuit_breaker_state: Dict[str, Any] = {
    "tripped": False,
    "trip_time": 0.0,
    "trip_date": "",
    "trip_reason": "",
    "daily_realized_loss": 0.0,
    "daily_unrealized_loss": 0.0,
    "baseline_equity": 0.0,
}


def _get_utc_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def is_circuit_breaker_active() -> Tuple[bool, str]:
    """Check if the daily loss circuit breaker is active."""
    global _circuit_breaker_state
    today = _get_utc_date_str()
    if _circuit_breaker_state.get("trip_date") != today:
        # Reset circuit breaker on new UTC day if not tripped today
        if _circuit_breaker_state.get("tripped") and _circuit_breaker_state.get("trip_date") != today:
            reset_circuit_breaker()

    if _circuit_breaker_state.get("tripped"):
        return True, _circuit_breaker_state.get("trip_reason", "Daily circuit breaker active")
    return False, ""


def trip_circuit_breaker(reason: str, daily_loss: float = 0.0, baseline_equity: float = 0.0) -> None:
    """Trip the daily circuit breaker to halt all new entries."""
    global _circuit_breaker_state
    _circuit_breaker_state["tripped"] = True
    _circuit_breaker_state["trip_time"] = time.time()
    _circuit_breaker_state["trip_date"] = _get_utc_date_str()
    _circuit_breaker_state["trip_reason"] = reason
    _circuit_breaker_state["daily_realized_loss"] = daily_loss
    _circuit_breaker_state["baseline_equity"] = baseline_equity
    log.error(f"🛑 [CIRCUIT-BREAKER] Tripped: {reason} (Daily Loss: ${daily_loss:.2f}, Baseline: ${baseline_equity:.2f})")


def reset_circuit_breaker() -> None:
    """Reset the daily circuit breaker."""
    global _circuit_breaker_state
    _circuit_breaker_state["tripped"] = False
    _circuit_breaker_state["trip_time"] = 0.0
    _circuit_breaker_state["trip_date"] = ""
    _circuit_breaker_state["trip_reason"] = ""
    _circuit_breaker_state["daily_realized_loss"] = 0.0
    _circuit_breaker_state["daily_unrealized_loss"] = 0.0
    _circuit_breaker_state["baseline_equity"] = 0.0
    log.info("🟢 [CIRCUIT-BREAKER] Circuit breaker reset.")


def get_circuit_breaker_state() -> Dict[str, Any]:
    """Return a copy of the circuit breaker state."""
    return dict(_circuit_breaker_state)


def calculate_portfolio_equity(
    spot_balances: Dict[str, float],
    prices: Dict[str, Any],
    futures_equity: float = 0.0
) -> float:
    """Calculate total portfolio equity across Spot and Futures balances."""
    usdt = float(spot_balances.get("usdt", 0.0))
    total = usdt + max(0.0, futures_equity)

    for asset, qty in spot_balances.items():
        if asset.lower() in ("usdt", "free_usdt", "locked_usdt"):
            continue
        if qty <= 0:
            continue
        sym = asset.upper()
        pair = sym if sym.endswith("USDT") else f"{sym}USDT"
        price_info = prices.get(pair, prices.get(sym, {}))
        price = 0.0
        if isinstance(price_info, dict):
            price = float(price_info.get("price", 0.0))
        elif isinstance(price_info, (int, float)):
            price = float(price_info)

        if price > 0:
            total += qty * price

    return total


def get_aggregate_stop_risk(
    open_positions: Dict[str, Any],
    default_sl_pct: float = STOP_LOSS_PCT
) -> float:
    """Calculate total potential stop-loss risk in USDT across all open positions."""
    total_risk_usdt = 0.0
    for pair, pos in open_positions.items():
        if not isinstance(pos, dict):
            continue
        qty = float(pos.get("qty", 0.0))
        entry_price = float(pos.get("entry_price", pos.get("entry", 0.0)))
        sl_pct = float(pos.get("stop_loss_pct", pos.get("hard_stop_pct", default_sl_pct)))
        notional = qty * entry_price
        if notional <= 0:
            notional = float(pos.get("notional", pos.get("total_usdt", 0.0)))
        
        pos_risk = notional * sl_pct
        total_risk_usdt += max(0.0, pos_risk)

    return total_risk_usdt


def check_entry_risk(
    symbol: str,
    side: str = "LONG",
    proposed_usdt: float = 0.0,
    price: float = 0.0,
    current_equity: float = 0.0,
    free_usdt: float = 0.0,
    open_positions: Optional[Dict[str, Any]] = None,
    is_futures: bool = False,
    stop_loss_pct: Optional[float] = None,
    daily_loss_usdt: float = 0.0,
    snapshot_time: Optional[float] = None,
    max_trade_risk_pct: Optional[float] = None,
    max_aggregate_risk_pct: Optional[float] = None,
    min_reserve_pct: Optional[float] = None,
    circuit_breaker_pct: Optional[float] = None,
    force_allow_futures: bool = False,
    snapshot_ttl_seconds: float = 60.0,
) -> RiskDecision:
    """
    Unified entry risk gate for all order-capable execution paths.
    
    Checks:
    1. Input validation and data sanity.
    2. Snapshot freshness.
    3. Futures entry disabled gate (fail closed unless explicitly enabled).
    4. Daily loss circuit breaker halt (2% limit).
    5. Minimum notional requirements.
    6. Minimum USDT cash reserve (25% equity).
    7. Per-trade risk budget cap (0.5% equity).
    8. Aggregate planned stop risk cap (2.0% equity).
    """
    snapshot_id = f"snap_{uuid.uuid4().hex[:8]}"
    sl_pct = stop_loss_pct if stop_loss_pct is not None else STOP_LOSS_PCT
    trade_risk_pct_cap = max_trade_risk_pct if max_trade_risk_pct is not None else RISK_BUDGET_PER_TRADE_PCT
    agg_risk_pct_cap = max_aggregate_risk_pct if max_aggregate_risk_pct is not None else MAX_AGGREGATE_STOP_RISK_PCT
    reserve_pct_req = min_reserve_pct if min_reserve_pct is not None else MIN_EQUITY_USDT_RESERVE_PCT
    cb_pct_limit = circuit_breaker_pct if circuit_breaker_pct is not None else DAILY_LOSS_CIRCUIT_BREAKER_PCT
    positions = open_positions if open_positions is not None else {}

    # 1. Input sanity check
    if price <= 0.0 or proposed_usdt <= 0.0 or current_equity <= 0.0:
        return RiskDecision(
            allowed=False,
            reason_code="INVALID_DATA",
            snapshot_id=snapshot_id,
            reason=f"Invalid pricing/equity inputs (price={price}, proposed_usdt={proposed_usdt}, equity={current_equity})",
        )

    # 2. Snapshot freshness check
    if snapshot_time is not None:
        age = time.time() - snapshot_time
        if age > snapshot_ttl_seconds:
            return RiskDecision(
                allowed=False,
                reason_code="STALE_SNAPSHOT",
                snapshot_id=snapshot_id,
                reason=f"Market snapshot is stale ({age:.1f}s > {snapshot_ttl_seconds:.1f}s TTL)",
            )

    # 3. Futures Entry Gate
    if is_futures:
        futures_active = FUTURES_ENABLED or force_allow_futures
        if not futures_active:
            log.warning(f"🛡️ [RISK-GATE] Futures entry rejected for {symbol}: Futures entries disabled by default.")
            return RiskDecision(
                allowed=False,
                reason_code="FUTURES_ENTRY_DISABLED",
                snapshot_id=snapshot_id,
                reason="Futures entries are disabled by default (FUTURES_ENABLED=False)",
            )

    # 4. Daily Loss Circuit Breaker Check
    cb_active, cb_reason = is_circuit_breaker_active()
    if cb_active:
        return RiskDecision(
            allowed=False,
            reason_code="CIRCUIT_BREAKER_ACTIVE",
            snapshot_id=snapshot_id,
            reason=f"Circuit breaker active: {cb_reason}",
        )

    if daily_loss_usdt > 0.0 and current_equity > 0.0:
        daily_loss_pct = daily_loss_usdt / current_equity
        if daily_loss_pct >= cb_pct_limit:
            trip_circuit_breaker(
                f"Daily loss ${daily_loss_usdt:.2f} ({daily_loss_pct*100:.1f}%) breached circuit breaker threshold ({cb_pct_limit*100:.1f}%)",
                daily_loss=daily_loss_usdt,
                baseline_equity=current_equity
            )
            return RiskDecision(
                allowed=False,
                reason_code="CIRCUIT_BREAKER_ACTIVE",
                snapshot_id=snapshot_id,
                reason=f"Daily loss limit breached ({daily_loss_pct*100:.1f}% >= {cb_pct_limit*100:.1f}%)",
            )

    # 5. Minimum Notional Check
    if proposed_usdt < MIN_TRADE_USDT:
        return RiskDecision(
            allowed=False,
            reason_code="MIN_NOTIONAL_EXCEEDS_BUDGET",
            snapshot_id=snapshot_id,
            reason=f"Proposed trade size ${proposed_usdt:.2f} is below minimum notional ${MIN_TRADE_USDT:.2f}",
        )

    # 6. Tradable Equity USDT Reserve Check (for spot entries)
    if not is_futures:
        remaining_usdt = free_usdt - proposed_usdt
        required_reserve_usdt = current_equity * reserve_pct_req
        if remaining_usdt < required_reserve_usdt:
            return RiskDecision(
                allowed=False,
                reason_code="INSUFFICIENT_RESERVE",
                snapshot_id=snapshot_id,
                reason=(
                    f"Insufficient USDT cash reserve: remaining ${remaining_usdt:.2f} "
                    f"< required ${required_reserve_usdt:.2f} ({reserve_pct_req*100:.0f}% equity reserve)"
                ),
                metadata={
                    "remaining_usdt": remaining_usdt,
                    "required_reserve_usdt": required_reserve_usdt,
                    "reserve_pct": reserve_pct_req,
                }
            )

    # 7. Per-Trade Risk Budget Check
    trade_risk_usdt = proposed_usdt * sl_pct
    max_trade_risk_usdt = current_equity * trade_risk_pct_cap
    if trade_risk_usdt > max_trade_risk_usdt + 1e-6:
        return RiskDecision(
            allowed=False,
            reason_code="RISK_BUDGET_EXCEEDED",
            snapshot_id=snapshot_id,
            reason=(
                f"Trade risk ${trade_risk_usdt:.2f} exceeds per-trade risk budget "
                f"${max_trade_risk_usdt:.2f} ({trade_risk_pct_cap*100:.2f}% equity)"
            ),
            metadata={
                "trade_risk_usdt": trade_risk_usdt,
                "max_trade_risk_usdt": max_trade_risk_usdt,
                "trade_risk_pct_cap": trade_risk_pct_cap,
            }
        )

    # 8. Aggregate Planned Stop Risk Cap Check
    existing_stop_risk = get_aggregate_stop_risk(positions, default_sl_pct=sl_pct)
    total_planned_stop_risk = existing_stop_risk + trade_risk_usdt
    max_aggregate_risk_usdt = current_equity * agg_risk_pct_cap

    if total_planned_stop_risk > max_aggregate_risk_usdt + 1e-6:
        return RiskDecision(
            allowed=False,
            reason_code="RISK_BUDGET_EXCEEDED",
            snapshot_id=snapshot_id,
            reason=(
                f"Aggregate portfolio stop risk ${total_planned_stop_risk:.2f} exceeds cap "
                f"${max_aggregate_risk_usdt:.2f} ({agg_risk_pct_cap*100:.1f}% equity)"
            ),
            metadata={
                "existing_stop_risk": existing_stop_risk,
                "trade_risk_usdt": trade_risk_usdt,
                "total_planned_stop_risk": total_planned_stop_risk,
                "max_aggregate_risk_usdt": max_aggregate_risk_usdt,
                "agg_risk_pct_cap": agg_risk_pct_cap,
            }
        )

    # All checks passed
    return RiskDecision(
        allowed=True,
        reason_code="ALLOW",
        snapshot_id=snapshot_id,
        reason="Entry risk approval granted",
        metadata={
            "proposed_usdt": proposed_usdt,
            "trade_risk_usdt": trade_risk_usdt,
            "total_planned_stop_risk": total_planned_stop_risk,
            "current_equity": current_equity,
        }
    )
