"""Standard technical indicators for the Hermes trading system.

All functions use pure Python (no numpy) and work with List[float] inputs.
"""

from typing import List


def ema(prices: List[float], period: int) -> List[float]:
    """Calculate EMA series using Wilder's smoothing method.

    Args:
        prices: List of price values.
        period: EMA period.

    Returns:
        List of EMA values (same length as input). The first (period-1)
        values are None placeholder since EMA needs warmup.
    """
    if len(prices) < period:
        return [None] * len(prices)

    result: List[float] = [None] * (period - 1)

    # First EMA value is simple SMA
    first_ema = sum(prices[:period]) / period
    result.append(first_ema)

    multiplier = 2.0 / (period + 1)

    for i in range(period, len(prices)):
        prev_ema = result[-1]
        current_ema = (prices[i] - prev_ema) * multiplier + prev_ema
        result.append(current_ema)

    return result


def calc_macd(
    prices: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict:
    """Calculate MACD (Moving Average Convergence Divergence).

    Args:
        prices: List of close prices.
        fast: Fast EMA period (default 12).
        slow: Slow EMA period (default 26).
        signal: Signal line EMA period (default 9).

    Returns:
        dict with keys: macd (float), signal (float), histogram (float).
        Returns None values if insufficient data.
    """
    if len(prices) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}

    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)

    # Build MACD line: fast EMA - slow EMA (aligned by index)
    macd_line: List[float] = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(fast_ema, slow_ema)
    ]

    # Filter to valid (non-None) MACD values for signal EMA
    valid_macd = [v for v in macd_line if v is not None]

    if len(valid_macd) < signal:
        return {"macd": None, "signal": None, "histogram": None}

    # Signal line = signal-period EMA of MACD line
    signal_ema = ema(valid_macd, signal)
    macd_val = valid_macd[-1]
    signal_val = signal_ema[-1] if signal_ema else None
    histogram = (macd_val - signal_val) if macd_val is not None and signal_val is not None else None

    return {
        "macd": macd_val,
        "signal": signal_val,
        "histogram": histogram,
    }


def calc_bollinger_bands(
    prices: List[float],
    period: int = 20,
    std_mult: float = 2.0,
) -> dict:
    """Calculate Bollinger Bands.

    Args:
        prices: List of close prices.
        period: SMA period (default 20).
        std_mult: Standard deviation multiplier (default 2.0).

    Returns:
        dict with keys: upper (float), middle (float), lower (float).
        Returns None values if insufficient data.
    """
    if len(prices) < period:
        return {"upper": None, "middle": None, "lower": None}

    # Middle band = SMA
    recent = prices[-period:]
    middle = sum(recent) / period

    # Standard deviation
    variance = sum((p - middle) ** 2 for p in recent) / period
    std = variance ** 0.5

    upper = middle + std * std_mult
    lower = middle - std * std_mult

    return {"upper": upper, "middle": middle, "lower": lower}


def _calc_tr(high: List[float], low: List[float], close: List[float]) -> List[float]:
    """Calculate True Range values for each bar.

    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    """
    tr_values: List[float] = []
    for i in range(len(high)):
        if i == 0:
            tr = high[0] - low[0]
        else:
            hl = high[i] - low[i]
            hc = abs(high[i] - close[i - 1])
            lc = abs(low[i] - close[i - 1])
            tr = max(hl, hc, lc)
        tr_values.append(tr)
    return tr_values


def calc_atr(
    high: List[float],
    low: List[float],
    close: List[float],
    period: int = 14,
) -> float:
    """Calculate Average True Range (ATR).

    Uses Wilder's smoothing method (same smoothing as RSI/EMA).

    Args:
        high: List of high prices.
        low: List of low prices.
        close: List of close prices.
        period: ATR period (default 14).

    Returns:
        ATR value as float, or 0.0 if insufficient data.
    """
    if len(high) < period + 1:
        return 0.0

    tr_values = _calc_tr(high, low, close)

    # First ATR = simple average of first 'period' TR values
    atr = sum(tr_values[:period]) / period

    # Wilder's smoothing: subsequent ATR values
    for i in range(period, len(tr_values)):
        atr = (atr * (period - 1) + tr_values[i]) / period

    return atr


def calc_supertrend(
    high: List[float],
    low: List[float],
    close: List[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> dict:
    """Calculate Supertrend indicator.

    Args:
        high: List of high prices.
        low: List of low prices.
        close: List of close prices.
        period: ATR period (default 10).
        multiplier: ATR multiplier for band width (default 3.0).

    Returns:
        dict with keys: supertrend (float), upper (float), lower (float).
        supertrend is the current supertrend value; direction flips when
        price crosses the bands.
    """
    if len(high) < period + 1:
        return {"supertrend": None, "upper": None, "lower": None}

    atr = calc_atr(high, low, close, period)
    tr_values = _calc_tr(high, low, close)

    # Initialize bands
    upper = close[0] + multiplier * atr
    lower = close[0] - multiplier * atr

    direction = 1  # 1 = bullish, -1 = bearish
    supertrend = lower

    for i in range(1, len(close)):
        price = close[i]

        # Update ATR progressively using Wilder's method
        atr = (atr * (period - 1) + tr_values[i]) / period

        # Compute raw bands
        raw_upper = price + multiplier * atr
        raw_lower = price - multiplier * atr

        # Smooth: upper band = min of raw upper and prior upper
        #         lower band = max of raw lower and prior lower
        upper = min(raw_upper, upper)
        lower = max(raw_lower, lower)

        # Check for direction flip
        if direction == 1 and price < upper:
            direction = -1
            supertrend = upper
        elif direction == -1 and price > lower:
            direction = 1
            supertrend = lower
        else:
            supertrend = upper if direction == 1 else lower

    return {
        "supertrend": supertrend,
        "upper": upper,
        "lower": lower,
    }


def _calc_dm(
    high: List[float],
    low: List[float],
) -> tuple:
    """Calculate Directional Movement (+DM and -DM).

    Returns:
        tuple of (plus_dm_list, minus_dm_list)
    """
    plus_dm: List[float] = []
    minus_dm: List[float] = []

    for i in range(1, len(high)):
        high_diff = high[i] - high[i - 1]
        low_diff = low[i - 1] - low[i]

        if high_diff > low_diff and high_diff > 0:
            pdm = high_diff
        else:
            pdm = 0.0

        if low_diff > high_diff and low_diff > 0:
            mdm = low_diff
        else:
            mdm = 0.0

        plus_dm.append(pdm)
        minus_dm.append(mdm)

    return plus_dm, minus_dm


def _smooth_dm(dm_values: List[float], period: int) -> float:
    """Wilder smooth a list of DM values and return final value."""
    if len(dm_values) < period:
        return sum(dm_values) / len(dm_values) if dm_values else 0.0

    # First value = simple sum
    smoothed = sum(dm_values[:period])

    # Wilder's smoothing
    for i in range(period, len(dm_values)):
        smoothed = (smoothed * (period - 1) + dm_values[i]) / period

    return smoothed


def calc_adx(
    high: List[float],
    low: List[float],
    close: List[float],
    period: int = 14,
) -> dict:
    """Calculate Average Directional Index (ADX).

    Args:
        high: List of high prices.
        low: List of low prices.
        close: List of close prices.
        period: ADX smoothing period (default 14).

    Returns:
        dict with keys: adx (float), plus_dm (float), minus_dm (float).
        Returns 0.0 for all values if insufficient data.
    """
    if len(high) < period + 1:
        return {"adx": 0.0, "plus_dm": 0.0, "minus_dm": 0.0}

    atr = calc_atr(high, low, close, period)

    plus_dm_list, minus_dm_list = _calc_dm(high, low)

    if len(plus_dm_list) < period:
        return {"adx": 0.0, "plus_dm": 0.0, "minus_dm": 0.0}

    # Smooth DM values
    smooth_plus_dm = _smooth_dm(plus_dm_list, period)
    smooth_minus_dm = _smooth_dm(minus_dm_list, period)

    # Avoid division by zero
    if atr == 0:
        return {"adx": 0.0, "plus_dm": smooth_plus_dm, "minus_dm": smooth_minus_dm}

    # Calculate +DI and -DI
    plus_di = (smooth_plus_dm / atr) * 100
    minus_di = (smooth_minus_dm / atr) * 100

    # DX = |+DI - -DI| / (+DI + -DI) * 100
    di_sum = plus_di + minus_di
    dx = abs(plus_di - minus_di) / di_sum * 100 if di_sum > 0 else 0.0

    # Build DX series for ADX smoothing
    dx_series: List[float] = []
    running_atr = atr
    dm_plus_running = smooth_plus_dm
    dm_minus_running = smooth_minus_dm

    for i in range(period, len(close)):
        tr_i = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        running_atr = (running_atr * (period - 1) + tr_i) / period

        pdm_i = plus_dm_list[i - 1] if i - 1 < len(plus_dm_list) else 0.0
        mdm_i = minus_dm_list[i - 1] if i - 1 < len(minus_dm_list) else 0.0

        dm_plus_running = (dm_plus_running * (period - 1) + pdm_i) / period
        dm_minus_running = (dm_minus_running * (period - 1) + mdm_i) / period

        if running_atr > 0:
            pdi = (dm_plus_running / running_atr) * 100
            mdi = (dm_minus_running / running_atr) * 100
            di_sum = pdi + mdi
            dx_i = abs(pdi - mdi) / di_sum * 100 if di_sum > 0 else 0.0
            dx_series.append(dx_i)

    # Smooth DX series to get ADX
    if len(dx_series) >= period:
        adx = sum(dx_series[:period]) / period
        for i in range(period, len(dx_series)):
            adx = (adx * (period - 1) + dx_series[i]) / period
    elif dx_series:
        adx = sum(dx_series) / len(dx_series)
    else:
        adx = dx

    return {
        "adx": adx,
        "plus_dm": smooth_plus_dm,
        "minus_dm": smooth_minus_dm,
    }
