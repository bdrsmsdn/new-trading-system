import json
import time
import asyncio
import websockets
from hermes.logging_setup import log
from hermes.state import prices
from hermes.config import WS_PAIRS

WS_URL = "wss://ws3.indodax.com/ws/"
WS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE5NDY2MTg0MTV9.UR1lBM6Eqh0yWz-PVirw1uPCxe60FdchR8eNVdsskeo"
WS_PAIRS_WS_FORMAT = [f"{p}idr" for p in WS_PAIRS]

def _ws_price_update(pair, data):
    """Callback to update global prices dict from WebSocket data."""
    prices[pair] = {
        "price": data["price"],
        "bid": data["bid"],
        "ask": data["ask"],
        "high": data.get("high"),
        "low": data.get("low"),
        "updated": time.time(),
        "source": data.get("source", "ws")
    }
    # Update 3m RSI from live WS price
    from hermes.indicators.rsi import update_rsi
    update_rsi(pair, data["price"])

class IndodaxWS:
    """Indodax WebSocket client for real-time price feeds."""

    def __init__(self, pairs=None, on_price=None):
        self.pairs = pairs or WS_PAIRS_WS_FORMAT
        self.on_price = on_price  # callback(pair, price_data)
        self.ws = None
        self.connected = False
        self.loop = None
        self.recv_task = None
        self.reconnect_delay = 5

    async def connect(self):
        self.ws = await websockets.connect(WS_URL, origin="https://indodax.com")
        self.connected = True
        self.reconnect_delay = 5
        # Auth
        await self.ws.send(json.dumps({"id": 1, "params": {"token": WS_TOKEN}}))
        await asyncio.sleep(1)
        # Subscribe to orderbook for each pair
        for pair in self.pairs:
            await self.ws.send(json.dumps({
                "id": 10 + self.pairs.index(pair),
                "method": 1,
                "params": {"channel": f"market:order-book-{pair}"}
            }))
            await asyncio.sleep(0.05)
        # Subscribe to 24h summary
        await self.ws.send(json.dumps({"id": 90, "method": 1, "params": {"channel": "market:summary-24h"}}))
        log.info(f"[WS] Connected and subscribed to {len(self.pairs)} pairs")

    async def listen(self):
        """Listen for messages indefinitely."""
        while self.connected:
            try:
                msg = await self.ws.recv()
                for line in msg.strip().split('\n'):
                    if line:
                        try:
                            self._handle_message(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            except websockets.ConnectionClosed:
                log.warning("[WS] Connection closed")
                break
            except Exception as e:
                log.error(f"[WS] Recv error: {e}")
                await asyncio.sleep(1)

    def _handle_message(self, msg):
        try:
            result = msg.get("result", {})
            channel = result.get("channel", "")
            data = result.get("data", {})

            if channel == "market:summary-24h":
                summary_data = data.get("data", data)
                if isinstance(summary_data, list):
                    for item in summary_data:
                        if item and len(item) >= 6:
                            symbol = item[0].lower().replace("idr", "")
                            pair_ws = f"{symbol}idr"
                            if pair_ws in self.pairs:
                                price = float(item[4])
                                if self.on_price:
                                    self.on_price(pair_ws.replace("idr",""), {
                                        "price": price,
                                        "bid": price,
                                        "ask": price,
                                        "high": float(item[2]),
                                        "low": float(item[3]),
                                        "vol": float(item[5]),
                                        "source": "ws_summary"
                                    })
                return

            if channel.startswith("market:order-book-"):
                pair_ws = channel.replace("market:order-book-", "")
                pair = pair_ws.replace("idr", "")
                book_data = data.get("data", data)
                if not isinstance(book_data, dict):
                    return
                asks = book_data.get("ask", [])
                bids = book_data.get("bid", [])
                if asks and bids:
                    best_ask = float(asks[0]["price"])
                    best_bid = float(bids[0]["price"])
                    mid = (best_ask + best_bid) / 2
                    if self.on_price:
                        self.on_price(pair, {
                            "price": mid,
                            "bid": best_bid,
                            "ask": best_ask,
                            "high": best_ask,
                            "low": best_bid,
                            "source": "ws_orderbook"
                        })
        except Exception as e:
            pass

    async def run_forever(self):
        """Connect, listen, reconnect loop."""
        while True:
            try:
                await self.connect()
                await self.listen()
            except Exception as e:
                log.error(f"[WS] Error: {e}")
            if self.connected:
                self.connected = False
            log.info(f"[WS] Reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)
            self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    def close(self):
        self.connected = False
        if self.ws:
            try:
                self.loop.create_task(self.ws.close())
            except:
                pass

ws_client = IndodaxWS(on_price=_ws_price_update)
