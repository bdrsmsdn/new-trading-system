import time
from typing import Dict
from hermes.logging_setup import log
from hermes.state import state
from hermes.api.auth import api_call

def get_balance(use_cache: bool = True) -> Dict[str, float]:
    """Get account balance (with caching to avoid nonce exhaustion)."""
    if use_cache and state.balance_cache:
        cache_age = time.time() - state.balance_cache_time
        if cache_age < 120:
            return state.balance_cache
    
    result = api_call("getInfo")
    if result.get("success") == 1:
        balances = result["return"].get("balance", {})
        state.balance_cache = {k: float(v) for k, v in balances.items() if float(v or 0) > 0}
        state.balance_cache_time = time.time()
        return state.balance_cache
    else:
        log.error(f"Balance fetch failed: {result.get('error', result)}")
        if state.balance_cache:
            log.warning("Returning stale balance cache due to API error")
            return state.balance_cache
        return {}
