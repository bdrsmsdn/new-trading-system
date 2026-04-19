import requests
import json
import time

# Re-test Binance WebSocket with more details
print("=" * 60)
print("Binance WebSocket (klines-dogeusdt-1h)")
try:
    import websocket
    ws = websocket.create_connection("wss://stream.binance.com:9443/ws", timeout=10)
    ws.send(json.dumps({"method": "SUBSCRIBE", "params": ["dogeusdt@kline_1h"], "id": 1}))
    time.sleep(3)
    resp = ws.recv()
    ws.close()
    print(f"  Response type: {type(resp)}")
    print(f"  Response repr: {repr(resp[:500])}")
    print(f"  Body: {resp[:200] if resp else 'EMPTY/NONE'}")
except ImportError:
    print("  Error: websocket-client library not installed")
except Exception as e:
    print(f"  Error: {e}")
