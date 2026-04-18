import math
from hermes.state import state
from hermes.config import MAX_TRADE_USDT, MIN_TRADE_USDT, FEE_BUFFER
from hermes.logging_setup import log

def calculate_volatility(pair: str, current_price: float, period: int = 14) -> float:
    """Calculate volatility using standard deviation of recent prices."""
    history = state.price_history.get(pair, [])
    if len(history) < period:
        return 1.0  # Not enough data, assume normal volatility
    
    recent = history[-period:]
    mean = sum(recent) / len(recent)
    
    variance = sum((p - mean) ** 2 for p in recent) / len(recent)
    stddev = math.sqrt(variance)
    
    if mean > 0:
        cv = stddev / mean
    else:
        cv = 0
    
    volatility_factor = max(0.3, min(1.0, 0.02 / cv if cv > 0 else 1.0))
    return volatility_factor

def calculate_atr(pair: str, current_price: float, period: int = 14) -> float:
    """Calculate Average True Range (ATR) as a volatility measure."""
    history = state.price_history.get(pair, [])
    if len(history) < period + 1:
        return current_price * 0.02
    
    tr_values = []
    for i in range(1, min(len(history), period + 1)):
        high_low = history[i] - history[i-1]
        tr = abs(high_low)
        tr_values.append(tr)
    
    if not tr_values:
        return current_price * 0.02
    
    atr = sum(tr_values) / len(tr_values)
    return atr

def get_dynamic_position_size(pair: str, current_price: float, usdt_balance: float) -> float:
    """Calculate dynamic position size based on volatility."""
    base_size = MAX_TRADE_USDT

    vol_factor = calculate_volatility(pair, current_price)
    atr = calculate_atr(pair, current_price)
    atr_ratio = atr / current_price if current_price > 0 else 0.02

    combined_factor = vol_factor * 0.7 + max(0.3, min(1.0, 0.02 / atr_ratio)) * 0.3
    dynamic_size = base_size * combined_factor

    max_affordable = usdt_balance * (1 - FEE_BUFFER)
    if max_affordable < MIN_TRADE_USDT:
        final_size = 0
    else:
        # Clamp: at least MIN_TRADE_USDT, at most max_affordable
        final_size = max(float(MIN_TRADE_USDT), min(dynamic_size, max_affordable))

    log.debug(f"Dynamic size for {pair}: ${final_size:.2f} (vol_factor={vol_factor:.2f}, combined={combined_factor:.2f})")
    return final_size
