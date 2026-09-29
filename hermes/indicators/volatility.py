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
    return atr if atr > 0 else (current_price * 0.02)

def get_dynamic_position_size(
    pair: str,
    current_price: float,
    usdt_balance: float,
    confidence: str = "Medium",
    score: int = 5
) -> float:
    """Calculate conviction-weighted and volatility-adjusted position size.
    
    Divides portfolio equity into balanced slots instead of dumping all capital on one coin,
    scales up size for high-conviction Grade A+ setups, and strictly rejects dust (< $5.50).
    """
    from hermes.config import TARGET_PORTFOLIO_SLOTS
    from hermes.api.balance import get_balance
    from hermes.state import prices

    # 1. Estimate Total Spot Portfolio Equity
    try:
        b = get_balance(use_cache=True)
        equity = float(usdt_balance)
        for coin, amount in b.items():
            if coin == "usdt" or amount <= 0:
                continue
            pr = prices.get(coin.upper(), {}).get("price", 0.0)
            if pr > 0:
                equity += amount * pr
    except Exception:
        equity = float(usdt_balance)

    # 2. Balanced Slot Sizing
    target_slots = max(1, TARGET_PORTFOLIO_SLOTS)
    slot_size = max(float(MIN_TRADE_USDT), equity / target_slots)

    # 3. Conviction-Based Multiplier
    conf_norm = str(confidence).capitalize()
    if conf_norm == "High" or score >= 8:
        conviction_mult = 1.25  # +25% weight for high conviction Grade A+
    elif conf_norm == "Medium" or score >= 5:
        conviction_mult = 1.00  # standard slot weight
    else:
        conviction_mult = 0.75  # lower weight for weaker setups

    base_size = min(float(MAX_TRADE_USDT), slot_size * conviction_mult)

    # 4. Volatility and ATR Adjustment
    vol_factor = calculate_volatility(pair, current_price)
    atr = calculate_atr(pair, current_price)
    atr_ratio = (atr / current_price) if current_price > 0 else 0.02

    atr_term = (0.02 / atr_ratio) if atr_ratio > 0 else 1.0
    combined_factor = vol_factor * 0.7 + max(0.3, min(1.0, atr_term)) * 0.3
    dynamic_size = base_size * combined_factor

    # 5. Clamp to affordable balance and preserve mandatory cash reserve (25% default)
    # Allows dynamic down-sizing so valid momentum entries aren't rejected outright
    min_reserve_pct = 0.25
    required_reserve = equity * min_reserve_pct
    max_spendable_with_reserve = max(0.0, (usdt_balance - required_reserve) * (1 - FEE_BUFFER))
    max_affordable = min(usdt_balance * (1 - FEE_BUFFER), max_spendable_with_reserve)

    if max_affordable < MIN_TRADE_USDT:
        log.debug(
            f"[SIZING] {pair}: max_affordable ${max_affordable:.2f} "
            f"(USDT: ${usdt_balance:.2f}, Reserve: ${required_reserve:.2f}) < MIN_TRADE_USDT ${MIN_TRADE_USDT:.2f}. "
            f"No trade without violating cash reserve."
        )
        return 0.0

    final_size = min(dynamic_size, max_affordable)
    if final_size < MIN_TRADE_USDT:
        log.debug(f"[SIZING] {pair}: final_size ${final_size:.2f} < MIN_TRADE_USDT ${MIN_TRADE_USDT:.2f}. Rejecting dust.")
        return 0.0

    log.info(
        f"[SIZING] {pair}: ${final_size:.2f} (Equity: ${equity:.2f}, Slot: ${slot_size:.2f}, "
        f"Conviction: {conf_norm}/{score} [x{conviction_mult}], Vol: {combined_factor:.2f})"
    )
    return round(final_size, 2)
