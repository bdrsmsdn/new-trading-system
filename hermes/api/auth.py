import json
import time
import hmac
import hashlib
import subprocess
import inspect
from pathlib import Path
from hermes.logging_setup import log, tracker

# Load from .env at project root (hermes/api/auth.py -> 3 levels up to project root)
_env = {}
try:
    env_file = Path(__file__).parent.parent.parent / ".env"
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            _env[k.strip()] = v.strip().strip("'\"")
except Exception:
    pass

API_KEY = _env.get("API_KEY", "")
API_SECRET = _env.get("API_SECRET", "")

# Use testnet if TESTNET=true in .env
_testnet = _env.get("TESTNET", "false").lower() == "true"
# Testnet base URL does NOT include /api (endpoints start with /api/v3)
# Mainnet base URL also does NOT include /api
BINANCE_API_BASE = "https://testnet.binance.vision" if _testnet else "https://api.binance.com"
BINANCE_SIGNED_ENDPOINTS = ["/api/v3/order", "/api/v3/account"]  # endpoints requiring signature

def get_server_time() -> int:
    """Fetch Binance server time in milliseconds."""
    try:
        result = subprocess.run(
            ["curl", "-s", f"{BINANCE_API_BASE}/api/v3/time"],
            capture_output=True, encoding='utf-8', errors='replace', timeout=10
        )
        data = json.loads(result.stdout)
        return int(data["serverTime"])
    except Exception as e:
        log.debug(f"Failed to get server time: {e}")
        return None

_last_timestamp_offset = 0

def binance_signed_request(endpoint: str, params: dict = None, method: str = "POST") -> dict:
    """Make a signed request to Binance API.

    Args:
        endpoint: Binance API endpoint (e.g., "/api/v3/order")
        params: Dictionary of parameters to sign and send
        method: "POST" or "GET"

    Returns:
        Parsed JSON response from Binance
    """
    global _last_timestamp_offset

    params = params or {}
    caller = inspect.stack()[1].function

    # Add timestamp (milliseconds)
    server_time = get_server_time()
    if server_time:
        local_time = int(time.time() * 1000)
        _last_timestamp_offset = server_time - local_time

    params["timestamp"] = int(time.time() * 1000) + _last_timestamp_offset

    # Build query string
    query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    # Sign with HMAC-SHA256
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    # Full URL with signature
    signed_query = f"{query_string}&signature={signature}"
    url = f"{BINANCE_API_BASE}{endpoint}"

    # For GET, append query string to URL. For POST, send as body.
    if method == "GET":
        url = f"{url}?{signed_query}"
        body = None
    else:
        body = signed_query

    for attempt in range(3):
        try:
            start_time = time.time()

            cmd = [
                "curl", "-s", "-X", method, url,
                "-H", f"X-MBX-APIKEY: {API_KEY}",
            ]
            if body:
                cmd += ["-d", body]

            result = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace', timeout=15)
            latency = (time.time() - start_time) * 1000

            body_response = result.stdout if result.stdout else ""
            http_status = "200"
            if "\n" in body_response:
                parts = body_response.rsplit("\n", 1)
                body_response = parts[0]
                http_status = parts[1].strip() if len(parts) == 2 else "200"

            # Log safe URL (no query string, no signature)
            safe_log_url = f"{BINANCE_API_BASE}{endpoint}"
            tracker.log_request(safe_log_url, method, caller, http_status, latency)

            # Handle timestamp error — adjust offset and retry
            try:
                resp_data = json.loads(body_response)
                if resp_data.get("code") == -1021:
                    # Timestamp invalid — resync
                    server_time = get_server_time()
                    if server_time:
                        _last_timestamp_offset = server_time - int(time.time() * 1000)
                    if attempt < 2:
                        continue  # retry with new offset
                elif resp_data.get("code"):
                    # Log error code only, not full response
                    log.error(f"Binance API error code: {resp_data.get('code')}, msg: {resp_data.get('msg', '')}")
            except json.JSONDecodeError:
                pass

            return resp_data if body_response else {}

        except Exception as e:
            log.error(f"Binance API call failed (attempt {attempt + 1}/3)")
            log.debug(f"Error details: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"success": 0, "error": str(e)}

    return {"success": 0, "error": "max retries exceeded"}

# Backwards compatibility alias for code that calls api_call()
def api_call(method: str, **params) -> dict:
    """Legacy compatibility wrapper — routes to binance_signed_request."""
    # Map legacy method names to Binance endpoints
    if method == "getInfo":
        return binance_signed_request("/api/v3/account", {}, method="GET")
    elif method == "trade":
        # params: pair, type (buy/sell), quantity, price, etc.
        # Build Binance order params
        symbol = params.get("pair", "").upper().replace("_", "")  # doge_idr -> DOGEIDR (but we use USDT)
        # For Binance: symbol should be like DOGEUSDT
        symbol = symbol.replace("IDR", "USDT") if "IDR" in symbol else f"{symbol}USDT"

        order_params = {
            "symbol": symbol,
            "side": params.get("type", "BUY").upper(),
            "type": "MARKET",  # We use MARKET orders
        }

        # For market buy, use quoteOrderQty for USDT amount
        if params.get("type", "").startswith("buy"):
            order_params["quoteOrderQty"] = params.get("idr", params.get("quoteOrderQty", 100))
        else:
            # For market sell, use quantity (coin amount)
            # Extract quantity from pair param (e.g., doge=1000)
            for k, v in params.items():
                if k not in ("pair", "type", "price", "idr"):
                    order_params["quantity"] = v
                    break

        return binance_signed_request("/api/v3/order", order_params, method="POST")

    return {"success": 0, "error": f"Unknown method: {method}"}
