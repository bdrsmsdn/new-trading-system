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


def get_spot_account_overview() -> dict:
    """Get complete spot wallet holdings with live prices, values in USDT, and dust categorization."""
    import urllib.request
    import json

    result = binance_signed_request("/api/v3/account", {}, method="GET")
    if "balances" not in result:
        return {"error": result.get("msg", "Failed to fetch spot balances")}

    # Get live ticker prices for conversion
    price_map = {}
    try:
        req = urllib.request.urlopen("https://api.binance.com/api/v3/ticker/price", timeout=5)
        prices_list = json.loads(req.read().decode())
        price_map = {p["symbol"]: float(p["price"]) for p in prices_list}
    except Exception as e:
        log.warning(f"Failed to fetch price list for spot overview: {e}")

    holdings = []
    dust = []
    total_usdt_value = 0.0

    for b in result.get("balances", []):
        free = float(b.get("free", 0) or 0)
        locked = float(b.get("locked", 0) or 0)
        total = free + locked
        if total <= 0.00000001:
            continue

        asset = b["asset"]
        usdt_val = 0.0

        if asset in ["USDT", "BUSD", "USDC", "FDUSD", "TUSD"]:
            usdt_val = total
        else:
            pair = f"{asset}USDT"
            if pair in price_map:
                usdt_val = total * price_map[pair]
            elif f"{asset}BTC" in price_map and "BTCUSDT" in price_map:
                usdt_val = total * price_map[f"{asset}BTC"] * price_map["BTCUSDT"]

        total_usdt_value += usdt_val
        item = {
            "asset": asset,
            "free": free,
            "locked": locked,
            "total": total,
            "usdt_value": round(usdt_val, 4),
            "price": price_map.get(f"{asset}USDT", 0.0)
        }

        if usdt_val < 1.0 and asset not in ["USDT", "BNB"]:
            dust.append(item)
        else:
            holdings.append(item)

    # Sort holdings descending by value
    holdings.sort(key=lambda x: x["usdt_value"], reverse=True)

    return {
        "total_portfolio_usdt": round(total_usdt_value, 2),
        "holdings": holdings,
        "dust_holdings": dust,
        "dust_count": len(dust)
    }

