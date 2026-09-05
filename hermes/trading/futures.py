"""Binance USDT-M Futures API Client & Execution Module for Hermes."""

import time
import hmac
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import json
from typing import Dict, Tuple, Optional, Any, List
from hermes.logging_setup import log
from hermes.api.auth import API_KEY, API_SECRET
from hermes.state import state

FUTURES_BASE_URL = "https://fapi.binance.com"

# Default Futures Settings
DEFAULT_LEVERAGE = 3           # Low leverage 3x (safe & measured)
DEFAULT_MARGIN_TYPE = "ISOLATED" # Always isolated margin to protect entire balance


def futures_signed_request(endpoint: str, params: Optional[dict] = None, method: str = "GET") -> dict:
    """Send a signed HMAC SHA256 request to Binance USD-M Futures (fapi.binance.com)."""
    if not API_KEY or not API_SECRET:
        log.error("[FUTURES] Missing API_KEY or API_SECRET in .env")
        return {"error": "Missing API Key/Secret"}

    params = params.copy() if params else {}
    params["timestamp"] = int(time.time() * 1000)

    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    full_query = f"{query_string}&signature={signature}"
    url = f"{FUTURES_BASE_URL}{endpoint}"

    headers = {
        "X-MBX-APIKEY": API_KEY,
        "User-Agent": "HermesTrader/2.0"
    }

    try:
        if method.upper() == "GET":
            req_url = f"{url}?{full_query}"
            req = urllib.request.Request(req_url, headers=headers, method="GET")
        elif method.upper() == "POST":
            data_bytes = full_query.encode("utf-8")
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
        elif method.upper() == "DELETE":
            req_url = f"{url}?{full_query}"
            req = urllib.request.Request(req_url, headers=headers, method="DELETE")
        else:
            return {"error": f"Unsupported method {method}"}

        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        log.error(f"[FUTURES HTTP ERROR] {e.code} on {endpoint}: {err_body}")
        try:
            return json.loads(err_body)
        except Exception:
            return {"error": err_body, "code": e.code}
    except Exception as e:
        log.error(f"[FUTURES ERROR] {endpoint}: {e}")
        return {"error": str(e)}


def get_futures_account_overview() -> dict:
    """Fetch complete USDT-M Futures account balances and open positions."""
    res = futures_signed_request("/fapi/v2/account", method="GET")
    if "error" in res or "code" in res and res.get("code") != 200:
        return {"success": False, "data": res}

    total_wallet = float(res.get("totalWalletBalance", 0.0))
    total_margin = float(res.get("totalMarginBalance", 0.0))
    available = float(res.get("availableBalance", 0.0))
    unrealized_pnl = float(res.get("totalUnrealizedProfit", 0.0))

    open_positions = []
    for pos in res.get("positions", []):
        amt = float(pos.get("positionAmt", 0.0))
        if amt != 0.0:
            open_positions.append({
                "symbol": pos.get("symbol"),
                "pair": pos.get("symbol", "").replace("USDT", ""),
                "side": "LONG" if amt > 0 else "SHORT",
                "amount": abs(amt),
                "entry_price": float(pos.get("entryPrice", 0.0)),
                "unrealized_pnl": float(pos.get("unrealizedProfit", 0.0)),
                "leverage": int(pos.get("leverage", 1)),
                "isolated": pos.get("isolated", True),
                "liquidation_price": float(pos.get("liquidationPrice", 0.0))
            })

    return {
        "success": True,
        "total_wallet_balance": total_wallet,
        "total_margin_balance": total_margin,
        "available_balance": available,
        "unrealized_pnl": unrealized_pnl,
        "open_positions": open_positions
    }


def set_leverage_and_margin(symbol: str, leverage: int = DEFAULT_LEVERAGE, margin_type: str = DEFAULT_MARGIN_TYPE) -> bool:
    """Set leverage and margin type (ISOLATED/CROSSED) for a symbol."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"

    # Set Margin Type (ignore if already set)
    try:
        futures_signed_request("/fapi/v1/marginType", {"symbol": sym, "marginType": margin_type}, method="POST")
    except Exception:
        pass

    # Set Leverage
    res = futures_signed_request("/fapi/v1/leverage", {"symbol": sym, "leverage": leverage}, method="POST")
    if res.get("leverage") == leverage:
        log.info(f"[FUTURES] Leverage for {sym} set to {leverage}x ({margin_type})")
        return True
    return False


def execute_futures_order(
    pair: str,
    side: str,           # "LONG" (BUY) or "SHORT" (SELL)
    usdt_margin: float,
    leverage: int = DEFAULT_LEVERAGE,
    order_type: str = "MARKET"
) -> Tuple[bool, dict]:
    """Execute a market or limit order on Binance USD-M Futures.
    
    Args:
        pair: e.g. "SOL", "DOGE", "XRP"
        side: "LONG" or "SHORT"
        usdt_margin: Notional margin in USDT (e.g. 10 USDT * 3x = $30 position)
        leverage: Default 3x
        order_type: "MARKET"
    """
    sym = pair.upper()
    symbol = sym if sym.endswith("USDT") else f"{sym}USDT"
    clean_pair = sym.replace("USDT", "")

    # Set Leverage & Isolated Margin first
    set_leverage_and_margin(symbol, leverage, DEFAULT_MARGIN_TYPE)

    # Fetch current mark price to compute exact quantity
    price_res = futures_signed_request("/fapi/v1/premiumIndex", {"symbol": symbol}, method="GET")
    mark_price = float(price_res.get("markPrice", 0.0))
    if mark_price <= 0:
        log.error(f"[FUTURES] Failed to get mark price for {symbol}")
        return False, {"error": "Invalid mark price"}

    notional_value = usdt_margin * leverage
    raw_qty = notional_value / mark_price

    # Precision formatting from pair precision map or symbol rules
    from hermes.config import PAIR_DECIMAL_PLACES
    decimals = PAIR_DECIMAL_PLACES.get(clean_pair, 2)
    if decimals == 0:
        quantity = f"{int(raw_qty)}"
    else:
        quantity = f"{round(raw_qty, decimals):.{decimals}f}"

    binance_side = "BUY" if side.upper() == "LONG" else "SELL"

    params = {
        "symbol": symbol,
        "side": binance_side,
        "type": order_type.upper(),
        "quantity": quantity
    }

    log.info(f"[FUTURES-EXEC] Opening {side} on {symbol}: Margin=${usdt_margin:.2f} ({leverage}x) -> Qty: {quantity} @ ~${mark_price}")
    res = futures_signed_request("/fapi/v1/order", params, method="POST")

    if "orderId" in res:
        log.info(f"✅ [FUTURES-SUCCESS] {side} {symbol} order #{res['orderId']} filled!")
        
        # Send Telegram alert
        try:
            from hermes.notifications.telegram import telegram_trade_alert
            telegram_trade_alert(pair=f"{clean_pair}-PERP", side=f"{side} ({leverage}x)", qty=float(quantity), price=mark_price, total=usdt_margin)
        except Exception as e:
            log.debug(f"Telegram alert error: {e}")

        return True, res
    else:
        log.error(f"❌ [FUTURES-FAILED] {symbol}: {res}")
        return False, res


def close_futures_position(pair: str, side: str, qty: float, reason: str = "Take Profit") -> Tuple[bool, dict]:
    """Close an open futures position (ReduceOnly market order in opposite direction)."""
    sym = pair.upper()
    symbol = sym if sym.endswith("USDT") else f"{sym}USDT"
    clean_pair = sym.replace("USDT", "")

    # To close LONG -> SELL; To close SHORT -> BUY
    close_side = "SELL" if side.upper() == "LONG" else "BUY"

    from hermes.config import PAIR_DECIMAL_PLACES
    decimals = PAIR_DECIMAL_PLACES.get(clean_pair, 2)
    if decimals == 0:
        quantity = f"{int(qty)}"
    else:
        quantity = f"{round(qty, decimals):.{decimals}f}"

    params = {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": quantity,
        "reduceOnly": "true"
    }

    log.info(f"[FUTURES-CLOSE] Closing {side} on {symbol} (Qty: {quantity}, Reason: {reason})")
    res = futures_signed_request("/fapi/v1/order", params, method="POST")

    if "orderId" in res:
        log.info(f"✅ [FUTURES-CLOSED] {symbol} closed successfully: {reason}")
        return True, res
    else:
        log.error(f"❌ [FUTURES-CLOSE-FAILED] {symbol}: {res}")
        return False, res
