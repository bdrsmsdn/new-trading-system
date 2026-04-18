import time
import asyncio
import json
import subprocess
import inspect
from typing import Optional, List, Dict
from hermes.logging_setup import log, tracker
from hermes.state import prices, _ticker_cache, _candle_cache
from hermes.config import ALL_TRACKED
from hermes.api import auth as _auth

# TTL per candle interval (seconds) — increased to reduce refetch frequency
CANDLE_TTL: Dict[str, int] = {
    "1d": 7200, "4h": 3600, "1h": 1800, "30m": 600,
    "15m": 600, "5m": 120, "3m": 120, "1m": 60,
}

# Binance interval mappings
_TF_MAP: Dict[str, str] = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "4h": "4h",
    "1d": "1d", "1w": "1w",
}
_INTERVAL_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400,
    "1d": 86400, "1w": 604800,
}

def _build_candles_url(pair: str, interval: str, limit: int) -> str:
    """Build Binance klines URL for candles."""
    tf = _TF_MAP.get(interval, "1h")
    return f"{_auth.BINANCE_API_BASE}/api/v3/klines?symbol={pair.upper()}USDT&interval={tf}&limit={limit}"

def _parse_candles(data) -> List[List[float]]:
    """Parse Binance klines response into [[ts, o, h, l, c, v]] format."""
    # Binance klines format:
    # [1499040000000, "0.0034", "0.0035", "0.0033", "0.0034", "123", ...]
    # [timestamp_ms, open, high, low, close, volume, ...]
    if not isinstance(data, list):
        return []
    result = []
    for c in data:
        if isinstance(c, list) and len(c) >= 6:
            result.append([
                float(c[0]),      # timestamp (ms)
                float(c[1]),      # open
                float(c[2]),      # high
                float(c[3]),      # low
                float(c[4]),      # close
                float(c[5]),      # volume
            ])
    return result

_PUBLIC_REST_MIN_INTERVAL = 0.5  # Binance is more generous, allow faster requests
_last_public_rest_call = 0.0

# ── Global REST budget: 1000 Binance requests per minute ──
_REST_BUDGET_MAX = 1000
_rest_budget_count = 0
_rest_budget_reset_time = 0.0

def _check_budget() -> bool:
    """Check if we have REST budget remaining."""
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
    global _last_public_rest_call
    caller = inspect.stack()[1].function

    # Budget check — if exhausted, return None immediately
    if not _check_budget():
        log.debug(f"[THROTTLE] REST budget exhausted ({_rest_budget_count}/{_REST_BUDGET_MAX}/min), skipping {url}")
        return None

    for attempt in range(max_retries):
        now = time.time()

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
                log.warning(f"[THROTTLE] HTTP 429 from {caller} — backing off (budget: {_rest_budget_count}/{_REST_BUDGET_MAX})")
                time.sleep(5)  # Simple backoff, no global cooldown
                continue

            return body
        except Exception as e:
            log.debug(f"[THROTTLE] Request failed for {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None

async def _async_throttled_public_get(url: str, timeout: int = 10, max_retries: int = 2) -> Optional[str]:
    """Async-safe throttled GET — uses await asyncio.sleep and asyncio.to_thread.
    Use this from daemon/event-loop contexts to avoid blocking the event loop."""
    global _last_public_rest_call
    caller = inspect.stack()[1].function

    if not _check_budget():
        log.debug(f"[THROTTLE] REST budget exhausted ({_rest_budget_count}/{_REST_BUDGET_MAX}/min), skipping {url}")
        return None

    for attempt in range(max_retries):
        now = time.time()

        elapsed = time.time() - _last_public_rest_call
        if elapsed < _PUBLIC_REST_MIN_INTERVAL:
            await asyncio.sleep(_PUBLIC_REST_MIN_INTERVAL - elapsed)

        _last_public_rest_call = time.time()
        start_time = time.time()

        try:
            result = await asyncio.to_thread(
                subprocess.run,
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
                log.warning(f"[THROTTLE] HTTP 429 from {caller} — backing off (budget: {_rest_budget_count}/{_REST_BUDGET_MAX})")
                await asyncio.sleep(5)
                continue

            return body
        except Exception as e:
            log.debug(f"[THROTTLE] Request failed for {url}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
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

    # Binance format: {"symbol": "DOGEUSDT", "price": "0.12345000"}
    body = _throttled_public_get(f"{_auth.BINANCE_API_BASE}/api/v3/ticker/price?symbol={pair.upper()}USDT")
    if body is None:
        return cached.get("price") if cached.get("price") else None
    try:
        data = json.loads(body)
        return float(data["price"])
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

    # Binance 24hr ticker: {"lastPrice": "0.1234", "highPrice": "0.1300", "lowPrice": "0.1200", "volume": "1000000", "quoteVolume": "10000"}
    body = _throttled_public_get(f"{_auth.BINANCE_API_BASE}/api/v3/ticker/24hr?symbol={pair.upper()}USDT")
    if body is None:
        return None
    try:
        data = json.loads(body)
        result = {
            "last": float(data["lastPrice"]),
            "high": float(data["highPrice"]),
            "low": float(data["lowPrice"]),
            "buy": float(data["lastPrice"]),  # Binance doesn't have separate bid/ask in 24hr
            "sell": float(data["lastPrice"]),
            "vol": float(data.get("volume", 0))
        }
        # Cache it
        _ticker_cache[pair] = {"high": result["high"], "low": result["low"], "ts": time.time()}
        return result
    except Exception as e:
        log.debug(f"Ticker fetch failed for {pair}: {e}")
        return None

def fetch_candles(pair: str, interval: str = "1h", limit: int = 100) -> Optional[List[List[float]]]:
    """Fetch OHLCV candles from Binance public API with TTL cache."""
    cache_key = f"{pair}_{interval}"
    ttl = CANDLE_TTL.get(interval, 600)
    cached = _candle_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < ttl:
        return cached["candles"]

    # Budget guard
    if not _check_budget():
        return cached["candles"] if cached else None

    body = _throttled_public_get(_build_candles_url(pair, interval, limit))
    if body is None:
        return cached["candles"] if cached else None

    try:
        data = json.loads(body)
        candles = _parse_candles(data)
        if candles:
            _candle_cache[cache_key] = {"candles": candles, "ts": time.time()}
            return candles
        return cached["candles"] if cached else None
    except Exception as e:
        log.debug(f"Candles fetch failed for {pair} ({interval}): {e}")
        return cached["candles"] if cached else None

async def fetch_candles_async(pair: str, interval: str = "1h", limit: int = 100) -> Optional[List[List[float]]]:
    """Async-safe version of fetch_candles for daemon/event-loop context."""
    cache_key = f"{pair}_{interval}"
    ttl = CANDLE_TTL.get(interval, 600)
    cached = _candle_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < ttl:
        return cached["candles"]

    if not _check_budget():
        return cached["candles"] if cached else None

    body = await _async_throttled_public_get(_build_candles_url(pair, interval, limit))
    if body is None:
        return cached["candles"] if cached else None

    try:
        data = json.loads(body)
        candles = _parse_candles(data)
        if candles:
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
    """Fetch prices via WS first, fallback to REST if WS fails."""
    import asyncio
    from hermes.api.websocket import ws_client

    log.info("Initializing prices using WebSocket feed (approx 8s)...")

    ws_success = False

    async def _fill_ws():
        nonlocal ws_success
        try:
            await ws_client.connect()
            listen_task = asyncio.create_task(ws_client.listen())
            await asyncio.sleep(6)
            ws_client.close()
            try:
                await listen_task
            except Exception:
                pass
            ws_success = len(prices) > 0
        except Exception as e:
            log.warning(f"WebSocket connection failed: {e}. Will use REST fallback.")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            pass
        else:
            loop.run_until_complete(_fill_ws())
    except RuntimeError:
        asyncio.run(_fill_ws())

    # If WS didn't get prices, use REST fallback for top pairs
    if not ws_success or len(prices) < 5:
        log.info("WS did not populate prices. Using REST fallback for priority pairs...")
        for pair in ["BTC", "ETH", "DOGE", "XRP", "SOL", "TON"]:
            price = fetch_price_rest(pair)
            if price:
                from hermes.state import prices as _prices
                _prices[pair] = {"price": price, "updated": time.time(), "source": "rest"}

    fetched = len(prices)
    missing = [p for p in ALL_TRACKED if p not in prices or not prices[p].get("price")]

    if missing and len(prices) < 10:
        log.info(f"Missing {len(missing)} pairs from price feed.")

    log.info(f"Price initialization complete. {fetched} pairs ready.")

def get_rest_budget_status() -> dict:
    """Return current REST budget status for debugging."""
    return {
        "used": _rest_budget_count,
        "max": _REST_BUDGET_MAX,
        "remaining": max(0, _REST_BUDGET_MAX - _rest_budget_count),
    }
