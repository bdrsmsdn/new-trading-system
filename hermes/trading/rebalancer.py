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
    idr = balance.get("idr", 0)
    
    total = idr
    holdings = {}
    for coin, amount in balance.items():
        if coin == "idr" or amount <= 0:
            continue
        price = prices.get(coin, {}).get("price", 0)
        if price and price > 0:
            val = amount * price
            holdings[coin] = val
            total += val
    
    if total <= 0:
        return {}
    
    return {coin: val / total for coin, val in holdings.items()}

def run_rebalance(get_balance_func):
    """Run a single iteration of portfolio allocation drift check and rebalance.
    Uses WS prices only — no REST calls."""
    try:
        regime, _ = get_market_regime()
        if regime == "BEAR":
            log.info("[REBALANCE] Skipping — BEAR regime, holding positions")
            return
        
        if not state.positions:
            log.info("[REBALANCE] No positions to rebalance")
            return
        
        alloc = get_portfolio_allocation(get_balance_func)
        if not alloc:
            return
        
        num_positions = len(state.positions)
        if num_positions == 0:
            return
        
        target_pct = 1.0 / num_positions
        
        log.info(f"[REBALANCE] Checking {num_positions} positions...")
        
        for pair, pos in state.positions.items():
            current_pct = alloc.get(pair, 0)
            drift = current_pct - target_pct
            
            if drift > REBALANCE_DRIFT_THRESHOLD:
                log.info(f"[REBALANCE] {pair.upper()}: {current_pct:.1%} (target: {target_pct:.1%}, drift: {drift:+.1%})")
                current_price = prices.get(pair, {}).get("price", 0)
                if current_price and current_price > pos["entry_price"]:
                    pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                    if pnl_pct >= 5:
                        log.info(f"[REBALANCE] Taking profit on {pair.upper()} ({pnl_pct:+.1f}%) to rebalance")
                        sell_qty = pos["qty"] * 0.3
                        if sell_qty > 0:
                            execute_sell(pair, current_price, sell_qty, "rebalance_drift")
        
        state.save()
        
    except Exception as e:
        log.error(f"[REBALANCE] Error: {e}")
