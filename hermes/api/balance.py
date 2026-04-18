import time
from typing import Dict
from hermes.logging_setup import log
from hermes.state import state
from hermes.api.auth import binance_signed_request

def get_balance(use_cache: bool = True) -> Dict[str, float]:
    """Get account balance from Binance (with caching to avoid rate limit).

    Returns dict with USDT and coin balances.
    Binance returns:
        {"balances": [{"asset": "BTC", "free": "0.123", "locked": "0"}, ...]}
    """
    if use_cache and state.balance_cache:
        cache_age = time.time() - state.balance_cache_time
        if cache_age < 120:  # 2 minute cache
            return state.balance_cache

    result = binance_signed_request("/api/v3/account", {}, method="GET")

    if "balances" in result:
        balances = {}
        for bal in result["balances"]:
            asset = bal["asset"]
            free = float(bal.get("free", 0) or 0)
            locked = float(bal.get("locked", 0) or 0)
            total = free + locked
            if total > 0:
                balances[asset.lower()] = total

        state.balance_cache = balances
        state.balance_cache_time = time.time()
        log.debug(f"Balance cache updated: {list(balances.keys())}")
        return balances
    else:
        error_msg = result.get("msg", str(result))
        log.error(f"Balance fetch failed: {error_msg}")
        if state.balance_cache:
            log.warning("Returning stale balance cache due to API error")
            return state.balance_cache
        return {}
