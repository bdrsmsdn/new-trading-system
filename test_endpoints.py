import requests
import json
import time

endpoints = [
    ("CoinGecko OHLC IDR", "https://api.coingecko.com/api/v3/coins/dogecoin/ohlc?vs_currency=idr&days=7"),
    ("CoinGecko OHLC USDT", "https://api.coingecko.com/api/v3/coins/dogecoin/ohlc?vs_currency=usdt&days=7"),
    ("CoinGecko history", "https://api.coingecko.com/api/v3/coins/dogecoin/market_chart?vs_currency=idr&days=1&interval=hourly"),
    ("Binance klines", "https://api.binance.com/api/v3/klines?symbol=DOGEUSDT&interval=1h&limit=10"),
]

print("=" * 60)
for name, url in endpoints:
    try:
        r = requests.get(url, timeout=15)
        body = r.text[:200]
        print(f"\n{name}")
        print(f"  Status: {r.status_code}")
        print(f"  Body: {body}")
    except Exception as e:
        print(f"\n{name}")
        print(f"  Error: {e}")

# WebSocket test
print("\n" + "=" * 60)
print("Binance WebSocket (klines-dogeusdt-1h)")
try:
    import websocket
    ws = websocket.create_connection("wss://stream.binance.com:9443/ws", timeout=10)
    ws.send(json.dumps({"method": "SUBSCRIBE", "params": ["dogeusdt@kline_1h"], "id": 1}))
    time.sleep(2)
    resp = ws.recv()
    ws.close()
    print(f"  Status: OK (received)")
    print(f"  Body: {resp[:200]}")
except ImportError:
    print("  Error: websocket-client library not installed")
except Exception as e:
    print(f"  Error: {e}")
