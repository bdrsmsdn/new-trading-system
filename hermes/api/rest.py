import time
import json
import subprocess
import inspect
from typing import Optional, List, Dict
from hermes.logging_setup import log, tracker
from hermes.state import prices, _ticker_cache, _candle_cache
from hermes.config import ALL_TRACKED

# TTL per candle interval (seconds) — increased to reduce refetch frequency
CANDLE_TTL: Dict[str, int] = {
    "1d": 7200, "4h": 3600, "1h": 1800, "30m": 600,
    "15m": 600, "5m": 120, "3m": 120, "1m": 60,
}

_PUBLIC_REST_MIN_INTERVAL = 1.5  # Conservative per-request interval
_last_public_rest_call = 0.0
_global_rate_limit_until = 0.0

# ── Global REST budget: hard cap of 80 Indodax requests per minute ──
_REST_BUDGET_MAX = 80
_rest_budget_count = 0
_rest_budget_reset_time = 0.0

def _check_budget() -> bool:
    """Check if we have REST budget remaining. Returns False if exhausted."""
    global _rest_budget_count, _rest_budget_reset_time
    now = time.time()
    if now - _rest_budget_reset_time > 60:
        _rest_budget_count = 0
        _rest_budget_reset_time = now
    return _rest_budget_count < _REST_BUDGET_MAX

def _consume_budget():
    """Consume one unit of REST budget."""
    global _rest_budget_count
    _rest_budget_count += 1

def _throttled_public_get(url: str, timeout: int = 10, max_retries: int = 2) -> Optional[str]:
    """Make a throttled public GET via curl with global budget."""
    global _last_public_rest_call, _global_rate_limit_until
    caller = inspect.stack()[1].function

    # Budget check — if exhausted, return None immediately
    if not _check_budget():
        log.debug(f"[THROTTLE] REST budget exhausted ({_rest_budget_count}/{_REST_BUDGET_MAX}/min), skipping {url}")
        return None

    for attempt in range(max_retries):
        now = time.time()
        if now < _global_rate_limit_until:
            wait = _global_rate_limit_until - now
            log.debug(f"[THROTTLE] Global cooldown active, waiting {wait:.0f}s")
            time.sleep(wait)

        elapsed = time.time() - _last_public_rest_call
        if elapsed < _PUBLIC_REST_MIN_INTERVAL:
            time.sleep(_PUBLIC_REST_MIN_INTERVAL - elapsed)

        _last_public_rest_call = time.time()
        start_time = time.time()

        try:
            result = subprocess.run(
                ["curl", "-s", "-A", "Mozilla/5.0", "-w", "\n%{http_code}", url],
                capture_output=True, text=True, timeout=timeout
            )
            latency = (time.time() - start_time) * 1000
            _consume_budget()

            parts = result.stdout.rsplit("\n", 1)
            body = parts[0] if len(parts) == 2 else result.stdout
            status_code = parts[1].strip() if len(parts) == 2 else "200"

            tracker.log_request(url, "GET", caller, status_code, latency)

            if status_code == "429":
                _global_rate_limit_until = time.time() + 300  # 5 minute cooldown immediately
                log.warning(f"[THROTTLE] HTTP 429 from {caller} — global cooldown 300s (budget: {_rest_budget_count}/{_REST_BUDGET_MAX})")
                return None

            return body
        except Exception as e:
            log.debug(f"[THROTTLE] Request failed for {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None

def fetch_price_rest(pair: str) -> Optional[float]:
    """Fetch price via public REST API. Prefers WS cache."""
    # Always check WS cache first
    cached = prices.get(pair, {})
    age = time.time() - cached.get("ts", cached.get("updated", 0))
    if age < 30 and cached.get("price"):
        return cached["price"]

    # Budget guard
    if not _check_budget():
        # Return stale price if available
        return cached.get("price") if cached.get("price") else None

    body = _throttled_public_get(f"https://indodax.com/api/ticker/{pair}_idr")
    if body is None:
        return cached.get("price") if cached.get("price") else None
    try:
        data = json.loads(body)
        return float(data["ticker"]["last"])
    except Exception as e:
        log.debug(f"REST price fetch failed for {pair}: {e}")
        return cached.get("price") if cached.get("price") else None

def fetch_ticker_full(pair: str) -> Optional[dict]:
    """Fetch full ticker data. Uses WS cache for high/low when available."""
    # Check WS prices for high/low first
    ws_data = prices.get(pair, {})
    if ws_data.get("high") and ws_data.get("low") and ws_data.get("price"):
        age = time.time() - ws_data.get("ts", ws_data.get("updated", 0))
        if age < 120:
            return {
                "last": ws_data["price"],
                "high": ws_data["high"],
                "low": ws_data["low"],
                "buy": ws_data.get("bid", ws_data["price"]),
                "sell": ws_data.get("ask", ws_data["price"]),
                "vol": ws_data.get("vol", 0)
            }

    # Check ticker cache
    cached_ticker = _ticker_cache.get(pair)
    if cached_ticker and (time.time() - cached_ticker.get("ts", 0)) < 600:
        return {
            "last": ws_data.get("price", 0),
            "high": cached_ticker["high"],
            "low": cached_ticker["low"],
            "buy": ws_data.get("bid", 0),
            "sell": ws_data.get("ask", 0),
            "vol": 0
        }

    # Budget guard — don't waste REST on ticker if budget low
    if not _check_budget():
        return None

    body = _throttled_public_get(f"https://indodax.com/api/ticker/{pair}_idr")
    if body is None:
        return None
    try:
        data = json.loads(body)
        t = data["ticker"]
        result = {
            "last": float(t["last"]),
            "high": float(t["high"]),
            "low": float(t["low"]),
            "buy": float(t["buy"]),
            "sell": float(t["sell"]),
            "vol": float(t.get(f"vol_{pair}", 0))
        }
        # Cache it
        _ticker_cache[pair] = {"high": result["high"], "low": result["low"], "ts": time.time()}
        return result
    except Exception as e:
        log.debug(f"Ticker fetch failed for {pair}: {e}")
        return None

def fetch_candles(pair: str, interval: str = "1h", limit: int = 100) -> Optional[List[List[float]]]:
    """Fetch OHLCV candles from Indodax public API with TTL cache."""
    cache_key = f"{pair}_{interval}"
    ttl = CANDLE_TTL.get(interval, 600)
    cached = _candle_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < ttl:
        return cached["candles"]

    # Budget guard
    if not _check_budget():
        return cached["candles"] if cached else None

    body = _throttled_public_get(
        f"https://indodax.com/api/klines/{pair}idr?interval={interval}&limit={limit}"
    )
    if body is None:
        return cached["candles"] if cached else None

    try:
        data = json.loads(body)
        if data.get("success") == 1:
            candles = data.get("klines", [])
            _candle_cache[cache_key] = {"candles": candles, "ts": time.time()}
            return candles
        return cached["candles"] if cached else None
    except Exception as e:
        log.debug(f"Candles fetch failed for {pair} ({interval}): {e}")
        return cached["candles"] if cached else None

def update_price(pair: str, price: float, source: str = "rest") -> None:
    """Update global prices cache."""
    from hermes.indicators.rsi import update_rsi
    prices[pair] = {"price": price, "ts": time.time(), "source": source}
    update_rsi(pair, price)

    from hermes.config import PRICE_CACHE
    try:
        PRICE_CACHE.write_text(json.dumps(prices))
    except Exception:
        pass

def fetch_all_prices() -> None:
    """Fetch prices relying on WS only. No REST fallback."""
    import asyncio
    from hermes.api.websocket import ws_client

    log.info("Initializing prices using WebSocket feed (approx 8s)...")

    async def _fill_ws():
        await ws_client.connect()
        listen_task = asyncio.create_task(ws_client.listen())
        await asyncio.sleep(6)
        ws_client.close()
        try:
            await listen_task
        except Exception:
            pass

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            pass
        else:
            loop.run_until_complete(_fill_ws())
    except RuntimeError:
        asyncio.run(_fill_ws())

    fetched = len(prices)
    missing = [p for p in ALL_TRACKED if p not in prices]

    if missing:
        log.info(f"Missing {len(missing)} pairs from WS. No REST fallback to avoid 429.")

    log.info(f"Price initialization complete. {fetched} pairs ready.")

def get_rest_budget_status() -> dict:
    """Return current REST budget status for debugging."""
    return {
        "used": _rest_budget_count,
        "max": _REST_BUDGET_MAX,
        "remaining": max(0, _REST_BUDGET_MAX - _rest_budget_count),
        "cooldown_active": time.time() < _global_rate_limit_until,
        "cooldown_remaining": max(0, _global_rate_limit_until - time.time()),
    }
