import time
import json
import subprocess
import inspect
from typing import Optional, List, Dict
from hermes.logging_setup import log, tracker
from hermes.state import prices, _ticker_cache, _candle_cache
from hermes.config import ALL_TRACKED

# TTL per candle interval (seconds)
CANDLE_TTL: Dict[str, int] = {
    "1d": 3600, "4h": 1800, "1h": 600, "30m": 300,
    "15m": 300, "5m": 60, "3m": 60, "1m": 30,
}

_PUBLIC_REST_MIN_INTERVAL = 1.0  # More conservative interval
_last_public_rest_call = 0.0
_global_rate_limit_until = 0.0

def _throttled_public_get(url: str, timeout: int = 10, max_retries: int = 3) -> Optional[str]:
    """Make a throttled public GET via curl."""
    global _last_public_rest_call, _global_rate_limit_until
    caller = inspect.stack()[1].function

    for attempt in range(max_retries):
        now = time.time()
        if now < _global_rate_limit_until:
            wait = _global_rate_limit_until - now
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
            
            parts = result.stdout.rsplit("\n", 1)
            body = parts[0] if len(parts) == 2 else result.stdout
            status_code = parts[1].strip() if len(parts) == 2 else "200"

            tracker.log_request(url, "GET", caller, status_code, latency)

            if status_code == "429":
                if attempt < max_retries - 1:
                    delay = 2 ** attempt
                    log.warning(f"[THROTTLE] HTTP 429 — retrying in {delay}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(delay)
                    continue
                else:
                    _global_rate_limit_until = time.time() + 300  # 5 minutes
                    log.warning(f"[THROTTLE] HTTP 429 after all retries — global cooldown 300s")
                    return None

            return body
        except Exception as e:
            log.debug(f"[THROTTLE] Request failed for {url}: {e}")
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                time.sleep(delay)
                continue
            return None

def fetch_price_rest(pair: str) -> Optional[float]:
    """Fetch price via public REST API."""
    cached = prices.get(pair, {})
    age = time.time() - cached.get("ts", cached.get("updated", 0))
    if age < 10 and cached.get("price"):
        return cached["price"]

    body = _throttled_public_get(f"https://indodax.com/api/ticker/{pair}_idr")
    if body is None:
        return None
    try:
        data = json.loads(body)
        return float(data["ticker"]["last"])
    except Exception as e:
        log.debug(f"REST price fetch failed for {pair}: {e}")
        return None

def fetch_ticker_full(pair: str) -> Optional[dict]:
    """Fetch full ticker data including high/low/volume (throttled)."""
    body = _throttled_public_get(f"https://indodax.com/api/ticker/{pair}_idr")
    if body is None:
        return None
    try:
        data = json.loads(body)
        t = data["ticker"]
        return {
            "last": float(t["last"]),
            "high": float(t["high"]),
            "low": float(t["low"]),
            "buy": float(t["buy"]),
            "sell": float(t["sell"]),
            "vol": float(t.get(f"vol_{pair}", 0))
        }
    except Exception as e:
        log.debug(f"Ticker fetch failed for {pair}: {e}")
        return None

def fetch_candles(pair: str, interval: str = "1h", limit: int = 100) -> Optional[List[List[float]]]:
    """Fetch OHLCV candles from Indodax public API with TTL cache."""
    cache_key = f"{pair}_{interval}"
    ttl = CANDLE_TTL.get(interval, 300)
    cached = _candle_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < ttl:
        return cached["candles"]

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
    # We delay importing rsi to avoid circular imports during setup
    from hermes.indicators.rsi import update_rsi
    prices[pair] = {"price": price, "ts": time.time(), "source": source}
    update_rsi(pair, price)
    
    from hermes.config import PRICE_CACHE
    try:
        PRICE_CACHE.write_text(json.dumps(prices))
    except Exception:
        pass

def fetch_all_prices() -> None:
    """Fetch prices relying primarily on WS, with fallback to REST only if necessary."""
    import asyncio
    from hermes.api.websocket import ws_client
    
    log.info("Initializing prices using WebSocket feed (approx 5-8s)...")
    
    async def _fill_ws():
        await ws_client.connect()
        listen_task = asyncio.create_task(ws_client.listen())
        await asyncio.sleep(4)
        ws_client.close()
        try:
            await listen_task
        except Exception:
            pass

    # Run the WS filler
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If already in an event loop (e.g., daemon), don't block
            pass
        else:
            loop.run_until_complete(_fill_ws())
    except RuntimeError:
        asyncio.run(_fill_ws())

    # Remove REST fallback entirely. If WS didn't catch it in 5s, we skip it for now.
    fetched = len(prices)
    missing = [p for p in ALL_TRACKED if p not in prices]
    
    if missing:
        log.info(f"Missing {len(missing)} pairs from WS. Skipping REST fallback to avoid 429.")
            
    log.info(f"Price initialization complete. {len(prices)} pairs ready.")
