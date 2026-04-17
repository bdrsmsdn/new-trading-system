"""Candle utilities and EMA calculations for multiple timeframes."""
import time
from typing import Optional, List, Dict, Tuple
from hermes.api.rest import fetch_candles, fetch_candles_async, _check_budget
from hermes.logging_setup import log

# TTL for candle data per timeframe (seconds)
CANDLE_TTL: Dict[str, int] = {
    "1m": 60,
    "5m": 120,
    "15m": 300,
    "30m": 600,
    "1h": 1800,
    "4h": 3600,
    "1d": 7200,
}

# Multi-RSI cache (shared with rsi.py)
_multi_rsi_cache: Dict[str, dict] = {}


def calc_ema(values: List[float], period: int) -> Optional[float]:
    """Calculate EMA from a list of values.
    
    Args:
        values: List of price values (oldest first)
        period: EMA period (e.g., 9, 21)
    
    Returns:
        EMA value or None if not enough data
    """
    if len(values) < period:
        return None
    
    # Use Wilder's smoothing factor (same as standard EMA)
    multiplier = 2.0 / (period + 1)
    
    # Start with SMA for first EMA value
    ema = sum(values[:period]) / period
    
    # Calculate EMA for remaining values
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
    
    return ema


def calc_ema_from_candles(candles: List[List[float]], period: int) -> Optional[float]:
    """Calculate EMA from OHLCV candles.
    
    Args:
        candles: List of [timestamp, open, high, low, close, vol] candles
        period: EMA period
    
    Returns:
        EMA value or None if not enough data
    """
    if len(candles) < period:
        return None
    
    closes = [float(c[4]) for c in candles]  # close is index 4
    return calc_ema(closes, period)


def get_candle_closes(candles: List[List[float]]) -> List[float]:
    """Extract close prices from candles."""
    return [float(c[4]) for c in candles]


def get_swing_low(candles: List[List[float]], lookback: int = 10) -> Optional[float]:
    """Find the lowest low in the recent candles (for stop loss).
    
    Args:
        candles: List of [timestamp, open, high, low, close, vol] candles
        lookback: Number of candles to check from most recent
    
    Returns:
        Lowest low price or None
    """
    if len(candles) < 2:
        return None
    
    # Look at last `lookback` candles for swing low
    relevant = candles[-lookback:] if len(candles) >= lookback else candles
    lows = [float(c[3]) for c in relevant]  # low is index 3
    return min(lows) if lows else None


def get_swing_high(candles: List[List[float]], lookback: int = 10) -> Optional[float]:
    """Find the highest high in the recent candles (for stop loss).
    
    Args:
        candles: List of [timestamp, open, high, low, close, vol] candles
        lookback: Number of candles to check from most recent
    
    Returns:
        Highest high price or None
    """
    if len(candles) < 2:
        return None
    
    # Look at last `lookback` candles for swing high
    relevant = candles[-lookback:] if len(candles) >= lookback else candles
    highs = [float(c[2]) for c in relevant]  # high is index 2
    return max(highs) if highs else None


def detect_ema_crossover(
    ema9_current: float, ema21_current: float,
    ema9_prev: float, ema21_prev: float
) -> Tuple[str, Optional[int]]:
    """Detect EMA crossover between EMA9 and EMA21.
    
    Args:
        ema9_current: Current EMA9 value
        ema21_current: Current EMA21 value
        ema9_prev: Previous candle EMA9 value
        ema21_prev: Previous candle EMA21 value
    
    Returns:
        Tuple of (crossover_type, candles_ago)
        crossover_type: "BULLISH", "BEARISH", or "NONE"
        candles_ago: How many candles ago the crossover happened (1=current, 2=one ago, etc.)
    """
    # Bullish: EMA9 crosses above EMA21
    # - Previous: EMA9 <= EMA21
    # - Current: EMA9 > EMA21
    prev_bullish = ema9_prev <= ema21_prev
    curr_bullish = ema9_current > ema21_current
    
    if prev_bullish and curr_bullish:
        return "BULLISH", 1
    
    # Bearish: EMA9 crosses below EMA21
    # - Previous: EMA9 >= EMA21
    # - Current: EMA9 < EMA21
    prev_bearish = ema9_prev >= ema21_prev
    curr_bearish = ema9_current < ema21_current
    
    if prev_bearish and curr_bearish:
        return "BEARISH", 1
    
    return "NONE", None


def detect_ema_crossover_history(
    ema9_values: List[float], ema21_values: List[float]
) -> Tuple[str, int]:
    """Detect EMA crossover and how many candles ago it happened.
    
    Args:
        ema9_values: List of EMA9 values (oldest first, index -1 = current)
        ema21_values: List of EMA21 values (oldest first, index -1 = current)
    
    Returns:
        Tuple of (crossover_type, candles_ago)
        candles_ago = 1 means current candle, 2 = one candle ago, etc.
        candles_ago = None means no recent crossover
    """
    if len(ema9_values) < 3 or len(ema21_values) < 3:
        return "NONE", None
    
    # Check last 3 candles for crossover
    for offset in range(1, 4):  # 1=current, 2=one ago, 3=two ago
        idx = -offset
        prev_idx = idx - 1
        
        ema9_curr = ema9_values[idx]
        ema21_curr = ema21_values[idx]
        ema9_prev = ema9_values[prev_idx]
        ema21_prev = ema21_values[prev_idx]
        
        if ema9_curr is None or ema21_curr is None or ema9_prev is None or ema21_prev is None:
            continue
        
        # Bullish crossover
        if ema9_prev <= ema21_prev and ema9_curr > ema21_curr:
            return "BULLISH", offset
        
        # Bearish crossover
        if ema9_prev >= ema21_prev and ema9_curr < ema21_curr:
            return "BEARISH", offset
    
    return "NONE", None


def calc_volatility(candles: List[List[float]], period: int = 14) -> Optional[float]:
    """Calculate price volatility (ATR-like measure).
    
    Returns:
        Average true range or None if not enough data
    """
    if len(candles) < period + 1:
        return None
    
    tr_values = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i-1][4])
        
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        tr_values.append(tr)
    
    return sum(tr_values[-period:]) / period if tr_values else None


def is_low_volatility(candles: List[List[float]], threshold_pct: float = 0.005) -> bool:
    """Check if market is in extremely low volatility condition.
    
    Args:
        candles: Recent candles
        threshold_pct: Volatility threshold as fraction of price (0.005 = 0.5%)
    
    Returns:
        True if volatility is extremely low (avoid signals)
    """
    if len(candles) < 20:
        return False
    
    closes = [float(c[4]) for c in candles[-20:]]
    if not closes:
        return False
    
    current_price = closes[-1]
    volatility = calc_volatility(candles[-20:])
    
    if volatility is None:
        return False
    
    # Low volatility if ATR is less than 0.5% of price
    return (volatility / current_price) < threshold_pct


def get_candle_data(
    pair: str,
    interval: str = "5m",
    limit: int = 100
) -> Optional[List[List[float]]]:
    """Fetch and cache candle data.
    
    Args:
        pair: Trading pair (e.g., 'doge', 'xrp')
        interval: Timeframe (1m, 5m, 15m, 1h, 4h, 1d)
        limit: Number of candles to fetch
    
    Returns:
        List of candles or None on error
    """
    return fetch_candles(pair, interval=interval, limit=limit)


async def get_candle_data_async(
    pair: str,
    interval: str = "5m",
    limit: int = 100
) -> Optional[List[List[float]]]:
    """Async version of get_candle_data.
    
    Args:
        pair: Trading pair
        interval: Timeframe
        limit: Number of candles
    
    Returns:
        List of candles or None on error
    """
    return await fetch_candles_async(pair, interval=interval, limit=limit)


def analyze_candles_multi_timeframe(
    pair: str,
    intervals: List[str] = None
) -> Dict[str, dict]:
    """Analyze candles across multiple timeframes.
    
    Args:
        pair: Trading pair
        intervals: List of timeframes to analyze
    
    Returns:
        Dict mapping interval -> {
            'candles': [...],
            'ema9': float,
            'ema21': float,
            'rsi': float,
            'swing_low': float,
            'swing_high': float,
            'volatility': float,
            'low_volatility': bool
        }
    """
    if intervals is None:
        intervals = ["5m", "15m"]
    
    result = {}
    
    for interval in intervals:
        candles = get_candle_data(pair, interval=interval, limit=100)
        
        if not candles or len(candles) < 25:
            continue
        
        closes = get_candle_closes(candles)
        ema9 = calc_ema(closes, 9)
        ema21 = calc_ema(closes, 21)
        swing_low = get_swing_low(candles)
        swing_high = get_swing_high(candles)
        volatility = calc_volatility(candles)
        low_vol = is_low_volatility(candles)
        
        # Calculate RSI from candles
        from hermes.indicators.rsi import calc_rsi_from_candles
        rsi = calc_rsi_from_candles(candles, period=14)
        
        result[interval] = {
            "candles": candles,
            "ema9": ema9,
            "ema21": ema21,
            "rsi": rsi,
            "swing_low": swing_low,
            "swing_high": swing_high,
            "volatility": volatility,
            "low_volatility": low_vol,
            "current_price": closes[-1] if closes else None,
        }
    
    return result
