import json
import time
import asyncio
import websockets
from pathlib import Path
from hermes.logging_setup import log
from hermes.state import prices

# Load TESTNET flag from .env at project root
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

_testnet = _env.get("TESTNET", "false").lower() == "true"
# Binance WebSocket URL (combined streams) - use testnet if enabled
# Mainnet: wss://stream.binance.com:9443/stream
# Testnet: wss://stream.testnet.binance.vision/stream
BINANCE_WS_URL = "wss://stream.testnet.binance.vision/stream" if _testnet else "wss://stream.binance.com:9443/stream"

# Format pairs for Binance WS: DOGE -> dogeusdt
def _pair_to_ws_symbol(pair: str) -> str:
    return f"{pair.lower()}usdt"

# Price history for spike detection (pair -> list of (timestamp, price))
_price_history = {}

def _ws_price_update(pair, data):
    """Callback to update global prices dict from WebSocket data and detect volatility spikes."""
    global _price_history
    cur_price = data.get("price")
    now = time.time()

    prices[pair] = {
        "price": cur_price,
        "bid": data.get("bid"),
        "ask": data.get("ask"),
        "high": data.get("high"),
        "low": data.get("low"),
        "vol": data.get("vol", 0),
        "updated": now,
        "source": data.get("source", "ws")
    }

    if cur_price and cur_price > 0:
        # Update RSI from live WS price
        from hermes.indicators.rsi import update_rsi
        update_rsi(pair, cur_price)

        # Volatility Spike Detection (1 minute rolling window)
        if pair not in _price_history:
            _price_history[pair] = []
        _price_history[pair].append((now, cur_price))
        # Keep only last 60 seconds
        _price_history[pair] = [(ts, p) for ts, p in _price_history[pair] if now - ts <= 60]

        if len(_price_history[pair]) >= 2:
            old_price = _price_history[pair][0][1]
            change_pct = (cur_price - old_price) / old_price
            from hermes.config import SPIKE_TRIGGER_PCT
            if abs(change_pct) >= SPIKE_TRIGGER_PCT:
                from hermes.state import state
                if not hasattr(state, "spike_events"):
                    state.spike_events = {}
                # Debounce spike triggers per pair (min 30s between triggers)
                spike_map = getattr(state, "spike_events", {})
                last_spike = spike_map.get(pair, 0)
                if now - last_spike > 30:
                    spike_map[pair] = now
                    setattr(state, "spike_events", spike_map)
                    direction = "SURGE 🚀" if change_pct > 0 else "DUMP 🔻"
                    log.info(f"[WS-SPIKE] {pair} {direction}: {change_pct*100:+.2f}% in {int(now - _price_history[pair][0][0])}s! Triggering fast evaluation.")


class BinanceWS:
    """Binance WebSocket client for real-time price feeds using Combined Streams."""

    def __init__(self, pairs=None, on_price=None):
        # Use provided pairs, or ALL_TRACKED from config (supports dynamic modification)
        from hermes.config import ALL_TRACKED as _tracked
        self.pairs = pairs or [_pair_to_ws_symbol(p) for p in _tracked]
        self.on_price = on_price  # callback(pair, price_data)
        self.ws = None
        self.connected = False
        self.loop = None
        self.recv_task = None
        self.reconnect_delay = 5
        self._extra_pairs = set()  # dynamically added pairs

    async def connect(self):
        # Build combined streams URL with @ticker suffix for each stream
        # Include both default pairs and dynamically added pairs
        all_pairs = list(self.pairs) + [p for p in self._extra_pairs if p not in self.pairs]
        streams = "/".join([f"{p}@ticker" for p in all_pairs])
        ws_url = f"{BINANCE_WS_URL}?streams={streams}"

        self.ws = await websockets.connect(ws_url)
        self.connected = True
        self.reconnect_delay = 5
        log.info(f"[WS] Connected and subscribed to Binance streams: {streams[:100]}...")

    async def listen(self):
        """Listen for messages indefinitely."""
        while self.connected:
            try:
                msg = await self.ws.recv()
                if msg:
                    try:
                        data = json.loads(msg)
                        self._handle_message(data)
                    except json.JSONDecodeError:
                        pass
            except websockets.ConnectionClosed:
                log.warning("[WS] Binance WS connection closed")
                break
            except Exception as e:
                log.error(f"[WS] Recv error: {e}")
                await asyncio.sleep(1)

    def _handle_message(self, msg):
        try:
            stream = msg.get("stream", "")
            data = msg.get("data", {})

            if not stream or not data:
                return

            # Extract symbol from stream: "dogeusdt@ticker" -> "dogeusdt" -> "DOGE"
            symbol_raw = stream.split("@")[0]  # "dogeusdt"
            pair = symbol_raw.upper().replace("USDT", "")  # "DOGE"

            # Parse ticker data
            # Binance 24hr ticker fields
            price = float(data.get("c", 0))  # close/last price
            high = float(data.get("h", 0))
            low = float(data.get("l", 0))
            volume = float(data.get("v", 0))
            bid = float(data.get("b", price))
            ask = float(data.get("a", price))

            if price <= 0:
                return

            # Update global prices dict
            prices[pair] = {
                "price": price,
                "bid": bid,
                "ask": ask,
                "high": high,
                "low": low,
                "vol": volume,
                "updated": time.time(),
                "source": "ws"
            }

            # Update RSI from live WS price
            from hermes.indicators.rsi import update_rsi
            update_rsi(pair, price)

            # Call on_price callback if provided
            if self.on_price:
                self.on_price(pair, {
                    "price": price,
                    "bid": bid,
                    "ask": ask,
                    "high": high,
                    "low": low,
                    "vol": volume,
                    "source": "ws"
                })
        except Exception as e:
            pass  # Silent fail for WS messages

    async def run_forever(self):
        """Connect, listen, reconnect loop."""
        while True:
            try:
                await self.connect()
                await self.listen()
            except Exception as e:
                log.error(f"[WS] Binance error: {e}")
            if self.connected:
                self.connected = False
            log.info(f"[WS] Binance reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)
            self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    def add_pair(self, pair: str) -> bool:
        """Add a pair to the WebSocket subscription dynamically.
        Returns True if added, False if already subscribed or WS not connected."""
        pair = pair.upper().replace("USDT", "")
        if pair in self.pairs or pair in self._extra_pairs:
            return False  # already subscribed

        self._extra_pairs.add(pair)
        ws_symbol = _pair_to_ws_symbol(pair)

        # If connected, need to reconnect to add the new pair
        if self.connected and self.ws:
            log.info(f"[WS] Adding new pair {pair} to subscription (will reconnect)...")
            # Close and let run_forever reconnect with new pairs
            self.connected = False
            try:
                if self.loop:
                    self.loop.create_task(self.ws.close())
            except:
                pass
            return True

        return True

    def get_subscribed_pairs(self) -> list:
        """Return list of currently subscribed pairs."""
        return list(self.pairs) + list(self._extra_pairs)

    def close(self):
        self.connected = False
        if self.ws:
            try:
                if self.loop:
                    self.loop.create_task(self.ws.close())
            except:
                pass

# Backward compatibility alias
ws_client = BinanceWS(on_price=_ws_price_update)

def ws_add_pair(pair: str) -> dict:
    """Add a pair to the WebSocket subscription dynamically.
    Agent can call this to add new pairs without restart.

    Args:
        pair: e.g. 'DOGE', 'DOGEUSDT', 'bitcoin'

    Returns:
        {"success": True/False, "subscribed": [...], "reason": ""}
    """
    result = ws_client.add_pair(pair)
    return {
        "success": result,
        "subscribed": ws_client.get_subscribed_pairs(),
        "reason": "Already subscribed" if not result else "Pair added, reconnecting..."
    }

def ws_get_pairs() -> list:
    """Get list of currently subscribed pairs."""
    return ws_client.get_subscribed_pairs()