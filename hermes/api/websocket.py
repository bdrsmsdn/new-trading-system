import json
import time
import asyncio
import websockets
from hermes.logging_setup import log
from hermes.state import prices

# Binance WebSocket URL (combined streams)
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"

# Format pairs for Binance WS: DOGE -> dogeusdt
def _pair_to_ws_symbol(pair: str) -> str:
    return f"{pair.lower()}usdt"

def _ws_price_update(pair, data):
    """Callback to update global prices dict from WebSocket data."""
    prices[pair] = {
        "price": data.get("price"),
        "bid": data.get("bid"),
        "ask": data.get("ask"),
        "high": data.get("high"),
        "low": data.get("low"),
        "vol": data.get("vol", 0),
        "updated": time.time(),
        "source": data.get("source", "ws")
    }
    # Update RSI from live WS price
    from hermes.indicators.rsi import update_rsi
    if data.get("price"):
        update_rsi(pair, data["price"])

class BinanceWS:
    """Binance WebSocket client for real-time price feeds using Combined Streams."""

    def __init__(self, pairs=None, on_price=None):
        self.pairs = pairs or [_pair_to_ws_symbol(p) for p in
                               ["DOGE", "XRP", "TON", "SOL", "BTC", "ETH", "BNB",
                                "PEPE", "SHIB", "ADA", "MATIC", "LINK", "AVAX",
                                "DOT", "BONK", "NEAR", "ALGO", "TRX"]]
        self.on_price = on_price  # callback(pair, price_data)
        self.ws = None
        self.connected = False
        self.loop = None
        self.recv_task = None
        self.reconnect_delay = 5

    async def connect(self):
        # Build combined streams URL
        streams = "/".join(self.pairs)
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