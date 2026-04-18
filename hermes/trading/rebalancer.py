import time
from typing import Dict
from hermes.logging_setup import log
from hermes.state import state, prices
from hermes.trading.execution import execute_sell
from hermes.indicators.signals import get_market_regime
from hermes.config import REBALANCE_DRIFT_THRESHOLD

def get_portfolio_allocation(get_balance_func) -> Dict[str, float]:
    """Calculate current portfolio allocation percentages. WS prices only."""
    balance = get_balance_func(use_cache=True)
    usdt = balance.get("usdt", 0)

    total = usdt
    holdings = {}
    for coin, amount in balance.items():
        if coin == "usdt" or amount <= 0:
            continue
        price = prices.get(coin, {}).get("price", 0)
        if price and price > 0:
            val = amount * price
            holdings[coin] = val
            total += val

    if total <= 0:
        return {"usdt": 100.0}

    allocations = {}
    if usdt > 0:
        allocations["usdt"] = (usdt / total) * 100
    for coin, val in holdings.items():
        allocations[coin] = (val / total) * 100

    return allocations

def run_rebalance(get_balance_func) -> None:
    """Check portfolio drift and rebalance if needed."""
    regime, _ = get_market_regime()
    if regime == "BEAR":
        log.info("[REBALANCE] Skipping in BEAR regime — preserve positions")
        return

    balance = get_balance_func(use_cache=True)
    usdt = balance.get("usdt", 0)
    holdings_value = 0

    for coin, amount in balance.items():
        if coin == "usdt" or amount <= 0:
            continue
        price = prices.get(coin, {}).get("price", 0)
        if price and price > 0:
            holdings_value += amount * price

    total = usdt + holdings_value
    if total <= 0:
        return

    target_pct = 100.0 / (1 + len([k for k in balance.keys() if k != "usdt" and balance[k] > 0]))

    for coin, amount in balance.items():
        if coin == "usdt" or amount <= 0:
            continue
        price = prices.get(coin, {}).get("price", 0)
        if not price or price <= 0:
            continue

        val = amount * price
        current_pct = (val / total) * 100 if total > 0 else 0
        drift = current_pct - target_pct

        if drift > REBALANCE_DRIFT_THRESHOLD * 100:
            pnl_pct = (price - state.positions.get(coin, {}).get("entry_price", 0)) / price * 100 if state.positions.get(coin) else 0
            if drift > 20 and pnl_pct >= 5:
                log.info(f"[REBALANCE] Taking partial profit on {coin}: {current_pct:.1f}% vs target {target_pct:.1f}%")
                sell_qty = amount * 0.25
                if sell_qty > 0:
                    execute_sell(coin, price, sell_qty, reason="rebalance_drift")