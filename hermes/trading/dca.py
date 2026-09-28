"""
DCA (Dollar Cost Averaging) and Grid Trading strategies for Hermes.

DCA: Automatically buys more of a coin when the price drops below a certain
threshold relative to the average entry price.

Grid: Places buy/sell orders at predefined price levels within a range.
"""
import time
from dataclasses import dataclass
from typing import Dict, Any

from hermes.logging_setup import log
from hermes.state import state
from hermes.trading.execution import execute_buy

# ─────────────────────────────────────────────────────────────────────────────
# State tracking (module-level, persists across calls)
# ─────────────────────────────────────────────────────────────────────────────

_dca_state: Dict[str, Dict[str, Any]] = {}
_grid_state: Dict[str, Dict[str, Any]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# DCA Config & Logic
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DCAConfig:
    """Configuration for Dollar Cost Averaging strategy."""
    trigger_pct: float      # Buy when price drops X% below avg entry (e.g., 0.05 = 5%)
    amount_pct: float       # How much of IDR balance to buy per DCA (e.g., 0.1 = 10%)
    max_dca_count: int      # Max DCA buys before giving up
    cooldown_minutes: int   # Minutes between DCA triggers


def run_dca(pair: str, config: DCAConfig, balance: float, current_price: float) -> dict:
    """
    Check if DCA trigger hit. If yes, execute buy.

    Returns dict with:
        triggered: bool - Whether DCA was triggered
        action: str - "buy", "skip_cooldown", "maxed_out", or None
        dca_count: int - Current DCA count for this pair
        avg_price: float - Average entry price
        new_entry: float - New average entry price (if triggered)
    """
    result = {
        "triggered": False,
        "action": None,
        "dca_count": 0,
        "avg_price": 0.0,
        "new_entry": 0.0,
    }

    # Initialize DCA state for pair if not present
    if pair not in _dca_state:
        _dca_state[pair] = {
            "count": 0,
            "avg_price": 0.0,
            "last_trigger": 0,
        }

    dca = _dca_state[pair]

    # Check if we have an open position
    if pair not in state.positions:
        result["action"] = None
        return result

    pos = state.positions[pair]
    entry_price = pos.get("entry_price", 0)
    qty = pos.get("qty", 0)

    if entry_price <= 0 or qty <= 0:
        result["action"] = None
        return result

    result["dca_count"] = dca["count"]
    result["avg_price"] = dca["avg_price"] if dca["avg_price"] > 0 else entry_price

    # Check cooldown
    cooldown_secs = config.cooldown_minutes * 60
    if dca["last_trigger"] > 0 and (time.time() - dca["last_trigger"]) < cooldown_secs:
        result["action"] = "skip_cooldown"
        return result

    # Check if maxed out
    if dca["count"] >= config.max_dca_count:
        result["action"] = "maxed_out"
        return result

    # Calculate trigger threshold
    avg_price = dca["avg_price"] if dca["avg_price"] > 0 else entry_price
    trigger_threshold = avg_price * (1 - config.trigger_pct)

    # Precedence: Disallow buying into a triggered stop / stop loss condition
    from hermes.config import STOP_LOSS_PCT
    sl_pct = float(pos.get("stop_loss_pct", STOP_LOSS_PCT))
    if current_price <= entry_price * (1.0 - sl_pct) or pos.get("state") in ("EXIT_PENDING", "CLOSED"):
        log.warning(f"[DCA] {pair.upper()}: Stop loss triggered or position exiting (price {current_price} <= SL {entry_price * (1.0 - sl_pct):.4f}). DCA buy rejected.")
        result["action"] = "rejected_triggered_stop"
        return result

    # Check if price dropped below trigger
    if current_price >= trigger_threshold:
        result["action"] = None
        return result

    # Trigger DCA buy
    buy_amount = balance * config.amount_pct
    if buy_amount < 10000:  # Minimum trade
        result["action"] = "insufficient_balance"
        return result

    log.info(f"[DCA] {pair.upper()}: Price {current_price:,.0f} < trigger {trigger_threshold:,.0f} "
             f"(avg: {avg_price:,.0f}). Buying Rp {buy_amount:,.0f}")

    success = execute_buy(pair, current_price, balance)

    if success:
        # Update DCA state
        dca["count"] += 1
        dca["last_trigger"] = time.time()

        # Update position average price
        new_pos = state.positions.get(pair)
        if new_pos:
            dca["avg_price"] = new_pos["entry_price"]

        result["triggered"] = True
        result["action"] = "buy"
        result["dca_count"] = dca["count"]
        result["new_entry"] = new_pos["entry_price"] if new_pos else current_price

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Grid Config & Logic
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GridConfig:
    """Configuration for Grid Trading strategy."""
    grid_levels: int        # Number of grid levels (e.g., 10)
    grid_spacing_pct: float # Spacing between levels (e.g., 0.02 = 2%)
    total_budget: float     # Total budget for grid (Rp)
    upper_bound: float      # Upper price band
    lower_bound: float      # Lower price band


def run_grid(pair: str, config: GridConfig, current_price: float) -> dict:
    """
    Check if price crossed a grid level, execute buy/sell at that level.

    Grid buys at lower levels (below mid), sells at upper levels (above mid).
    Each level gets an equal slice of the budget.

    Returns dict with:
        action: str - "buy", "sell", or None
        level: int - Grid level crossed (0 to grid_levels-1)
        filled_qty: float - Quantity filled
        filled_price: float - Price at fill
    """
    result = {
        "action": None,
        "level": -1,
        "filled_qty": 0.0,
        "filled_price": 0.0,
    }

    if config.grid_levels <= 0 or config.upper_bound <= config.lower_bound:
        return result

    # Initialize grid state for pair if not present
    if pair not in _grid_state:
        _grid_state[pair] = {
            "filled_levels": {},   # {level_index: qty_filled}
            "last_price": 0.0,
            "budget_per_level": config.total_budget / config.grid_levels,
        }

    gstate = _grid_state[pair]

    # Calculate grid step and levels
    price_range = config.upper_bound - config.lower_bound
    grid_step = price_range / config.grid_levels

    # Determine which level we're at
    if current_price <= config.lower_bound:
        current_level = 0
    elif current_price >= config.upper_bound:
        current_level = config.grid_levels - 1
    else:
        current_level = int((current_price - config.lower_bound) / grid_step)

    last_price = gstate.get("last_price", 0.0)
    result["level"] = current_level

    # No action if price hasn't moved to a new level
    if last_price == 0:
        gstate["last_price"] = current_price
        return result

    # Determine which level the last price was at
    if last_price <= config.lower_bound:
        last_level = 0
    elif last_price >= config.upper_bound:
        last_level = config.grid_levels - 1
    else:
        last_level = int((last_price - config.lower_bound) / grid_step)

    # No action if price hasn't moved to a new level
    if current_level == last_level:
        gstate["last_price"] = current_price
        return result

    # Mid point of the grid
    mid_level = config.grid_levels // 2

    if current_price < last_price:
        # Price dropped — check lower levels for buy
        for lvl in range(current_level, min(last_level, config.grid_levels)):
            if lvl not in gstate["filled_levels"]:
                # Buy at this level
                budget = gstate.get("budget_per_level", config.total_budget / config.grid_levels)
                qty = budget / (config.lower_bound + (lvl + 0.5) * grid_step)
                price = config.lower_bound + (lvl + 0.5) * grid_step

                success = execute_buy(pair, price, budget)
                if success:
                    gstate["filled_levels"][lvl] = qty
                    result["action"] = "buy"
                    result["filled_qty"] = qty
                    result["filled_price"] = price
                    log.info(f"[GRID] {pair.upper()}: BUY at level {lvl}, "
                             f"price {price:,.0f}, qty {qty:,.8f}")
                    break

    elif current_price > last_price:
        # Price rose — check upper levels for sell
        for lvl in range(max(last_level + 1, mid_level), current_level + 1):
            if lvl in gstate["filled_levels"]:
                qty = gstate["filled_levels"][lvl]
                price = config.lower_bound + (lvl + 0.5) * grid_step

                log.info(f"[GRID] {pair.upper()}: SELL opportunity at level {lvl}, "
                         f"price {price:,.0f}, qty {qty:,.8f}")
                result["action"] = "sell"
                result["filled_qty"] = qty
                result["filled_price"] = price
                break

    gstate["last_price"] = current_price
    return result


def reset_dca_state(pair: str) -> None:
    """Reset DCA state for a pair (e.g., when position is closed)."""
    if pair in _dca_state:
        del _dca_state[pair]


def reset_grid_state(pair: str) -> None:
    """Reset grid state for a pair."""
    if pair in _grid_state:
        del _grid_state[pair]
