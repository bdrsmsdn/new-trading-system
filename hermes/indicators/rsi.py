import time
from typing import Optional, List, Dict
from hermes.state import state, _multi_rsi_cache
from hermes.api.rest import fetch_candles

_MULTI_RSI_TTL = 300  # 5 minutes — reduced refetch frequency

def calc_rsi_from_candles(candles: List[List[float]], period: int = 14) -> Optional[float]:
    """Calculate RSI from candle close prices."""
    if len(candles) < period + 1:
        return None
    
    closes = [float(c[4]) for c in candles]  # close is index 4
    
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def update_rsi(pair: str, current_price: float, period: int = 3) -> float:
    """Update RSI using Wilder's smoothing method."""
    rs_state = state.rsi_state.get(pair, {
        "avg_gain": 0, "avg_loss": 0,
        "last_price": 0, "initialized": False
    })
    
    history = state.price_history.setdefault(pair, [])
    history.append(current_price)
    if len(history) > 100:
        history = history[-100:]
        state.price_history[pair] = history
    
    if not rs_state["initialized"]:
        if rs_state["last_price"] > 0:
            change = current_price - rs_state["last_price"]
            gain = max(change, 0)
            loss = max(-change, 0)
            rs_state["avg_gain"] = gain
            rs_state["avg_loss"] = loss
            rs_state["initialized"] = len(history) >= period
        rs_state["last_price"] = current_price
        state.rsi_state[pair] = rs_state
        return 50.0
    
    change = current_price - rs_state["last_price"]
    gain = max(change, 0)
    loss = max(-change, 0)
    
    n = period
    rs_state["avg_gain"] = (rs_state["avg_gain"] * (n - 1) + gain) / n
    rs_state["avg_loss"] = (rs_state["avg_loss"] * (n - 1) + loss) / n
    rs_state["last_price"] = current_price
    state.rsi_state[pair] = rs_state
    
    if rs_state["avg_loss"] == 0:
        return 100.0
    
    rs = rs_state["avg_gain"] / rs_state["avg_loss"]
    return 100 - (100 / (1 + rs))

def get_rsi(pair: str) -> float:
    """Get current RSI for a pair."""
    rs_state = state.rsi_state.get(pair, {})
    avg_gain = rs_state.get("avg_gain", 0)
    avg_loss = rs_state.get("avg_loss", 0)
    
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_multi_rsi(pair: str, price: float) -> Dict[str, float]:
    """Get RSI across multiple timeframes. Only fetches 1h and 4h to save REST budget."""
    # Always update 3m RSI from live price (no REST needed)
    rsi_3m = update_rsi(pair, price, period=3)

    # Return cached result if fresh
    cached = _multi_rsi_cache.get(pair)
    if cached and (time.time() - cached["ts"]) < _MULTI_RSI_TTL:
        result = dict(cached["rsi"])
        result["3m"] = rsi_3m
        return result

    result = {
        "3m": rsi_3m,
        "1h": 50.0,
        "4h": 50.0,
    }

    # Only fetch 2 timeframes instead of 4 — cuts REST calls in half
    timeframe_map = {
        "1h": ("1h", 14),
        "4h": ("4h", 14),
    }

    for key, (interval, period) in timeframe_map.items():
        candles = fetch_candles(pair, interval=interval, limit=100)
        if candles:
            rsi = calc_rsi_from_candles(candles, period=period)
            if rsi is not None:
                result[key] = rsi

    _multi_rsi_cache[pair] = {"rsi": dict(result), "ts": time.time()}
    return result

def calc_classic_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Calculate classic RSI from price list (for analysis mode)."""
    if len(prices) < period + 1:
        return None
    
    gains = []
    losses = []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i-1]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
