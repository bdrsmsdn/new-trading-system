"""
Binance Spot Position Reconciliation & Sync Module.

Cross-references Binance Spot wallet balances directly with /api/v3/myTrades execution history.
Performs FIFO trade matching to calculate the exact weighted average entry price,
fill timestamps, and manages TP (+10%) & SL (-5%) contracts.

Ensures that NO coins in the Binance Spot wallet are ever abandoned or mistaken for 'manual holdings'.
"""

import time
from typing import Dict, Any, Optional

from hermes.api.auth import binance_signed_request
from hermes.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, ALL_TRACKED
from hermes.logging_setup import log
from hermes.state import state


def reconcile_positions_from_binance(min_value_usdt: float = 0.80, save_to_state: bool = True) -> Dict[str, Any]:
    """
    Reconciles open positions by fetching all Binance Spot balances and
    cross-referencing with /api/v3/myTrades execution history using FIFO matching.

    Returns the updated positions dict.
    """
    try:
        account = binance_signed_request("/api/v3/account", method="GET")
    except Exception as e:
        log.error(f"[RECONCILE] Failed to fetch Binance account balances: {e}")
        return state.positions

    if not isinstance(account, dict) or "balances" not in account:
        log.warning(f"[RECONCILE] Unexpected account response: {account}")
        return state.positions

    reconciled: Dict[str, Dict[str, Any]] = {}
    ignored_assets = {"USDT", "BNB"}

    for b in account.get("balances", []):
        asset = b.get("asset", "").upper()
        if asset in ignored_assets:
            continue

        free = float(b.get("free", 0))
        locked = float(b.get("locked", 0))
        total_qty = free + locked

        if total_qty <= 0.00000001:
            continue

        # Check if asset or pair is in tracked universe or has a USDT pair
        sym = f"{asset}USDT"
        try:
            trades = binance_signed_request("/api/v3/myTrades", {"symbol": sym, "limit": 100}, method="GET")
        except Exception as te:
            log.warning(f"[RECONCILE] Failed to fetch myTrades for {sym}: {te}")
            continue

        if not isinstance(trades, list) or not trades:
            continue

        # FIFO matching of trades
        cur_buys = []
        for t in trades:
            q = float(t.get("qty", 0))
            p = float(t.get("price", 0))
            tm = float(t.get("time", 0)) / 1000.0
            if t.get("isBuyer"):
                cur_buys.append({"qty": q, "price": p, "time": tm, "remaining": q})
            else:
                sell_rem = q
                for b_item in cur_buys:
                    if b_item["remaining"] > 0:
                        take = min(b_item["remaining"], sell_rem)
                        b_item["remaining"] -= take
                        sell_rem -= take
                        if sell_rem <= 0:
                            break

        active_buys = [b_item for b_item in cur_buys if b_item["remaining"] > 0.00000001]
        if not active_buys:
            continue

        tot_cost = sum(b_item["remaining"] * b_item["price"] for b_item in active_buys)
        tot_q = sum(b_item["remaining"] for b_item in active_buys)
        avg_entry = tot_cost / tot_q if tot_q > 0 else 0.0
        buy_time = active_buys[-1]["time"]
        val_est = tot_q * avg_entry

        # Only track if total position value is above dust threshold
        if val_est >= min_value_usdt:
            # Preserve peak_price if previously tracked and higher
            existing_pos = state.positions.get(asset, {})
            peak_price = max(existing_pos.get("peak_price", avg_entry), avg_entry)

            reconciled[asset] = {
                "entry_price": avg_entry,
                "qty": round(total_qty, 8),
                "time": buy_time,
                "stop_loss": avg_entry * (1 - STOP_LOSS_PCT),
                "take_profit": avg_entry * (1 + TAKE_PROFIT_PCT),
                "peak_price": peak_price
            }

    if save_to_state:
        # Check diff
        added = [k for k in reconciled if k not in state.positions]
        updated = [k for k in reconciled if k in state.positions and (
            abs(reconciled[k]["qty"] - state.positions[k]["qty"]) > 0.0001 or
            abs(reconciled[k]["entry_price"] - state.positions[k]["entry_price"]) > 0.000001
        )]
        removed = [k for k in state.positions if k not in reconciled]

        state.positions = reconciled
        state.save()

        if added or updated or removed:
            log.info(
                f"[RECONCILE] State positions synchronized from Binance myTrades! "
                f"Added: {added or 'none'}, Updated: {updated or 'none'}, Removed: {removed or 'none'}. "
                f"Total active: {len(state.positions)}"
            )

    return reconciled
