import time
from typing import Tuple, List, Dict
from hermes.state import state, _ticker_cache
from hermes.api.rest import fetch_ticker_full
from hermes.indicators.rsi import get_rsi
from hermes.config import (
    FG_BUY_THRESHOLD, FG_STRONG_BUY, FG_SELL_THRESHOLD,
    RSI_BUY_THRESHOLD, RSI_STRONG_BUY, RSI_SELL_THRESHOLD,
    DAILY_POS_BUY, DAILY_POS_SELL
)

def get_daily_position(pair: str, current_price: float = None) -> float:
    """Calculate where current price sits in daily range (0-100%)."""
    ticker_data = _ticker_cache.get(pair, {})
    ticker_age = time.time() - ticker_data.get("ts", 0) if ticker_data else 999
    
    if ticker_age < 300 and current_price is not None:
        high = ticker_data.get("high", 0)
        low = ticker_data.get("low", 0)
        if high > low:
            return ((current_price - low) / (high - low)) * 100
        return 50.0

    ticker = fetch_ticker_full(pair)
    if not ticker:
        return 50.0

    if pair not in _ticker_cache:
        _ticker_cache[pair] = {}
    _ticker_cache[pair] = {"high": ticker["high"], "low": ticker["low"], "ts": time.time()}

    high = ticker["high"]
    low = ticker["low"]
    if high == low:
        return 50.0

    return ((current_price - low) / (high - low)) * 100

def get_market_regime() -> Tuple[str, str]:
    """Detect current market regime: BULL, BEAR, or SIDEWAYS."""
    fg = state.fg_value
    if fg >= 60:
        return "BULL", f"Fear & Greed at {fg} — Greed market"
    elif fg <= 30:
        return "BEAR", f"Fear & Greed at {fg} — Fear market"
    else:
        return "SIDEWAYS", f"Fear & Greed at {fg} — Neutral market"

def get_regime_trading_config() -> dict:
    """Return trading config adjustments based on market regime."""
    regime, desc = get_market_regime()
    if regime == "BULL":
        return {
            "max_active_multiplier": 1.5,
            "position_size_mult": 1.2,
            "tp_adjust": 1.0,
            "sl_adjust": 1.2,
            "allow_sell": True,
        }
    elif regime == "BEAR":
        return {
            "max_active_multiplier": 0.5,
            "position_size_mult": 0.5,
            "tp_adjust": 1.0,
            "sl_adjust": 0.8,
            "allow_sell": False,
        }
    else:
        return {
            "max_active_multiplier": 1.0,
            "position_size_mult": 0.8,
            "tp_adjust": 1.0,
            "sl_adjust": 1.0,
            "allow_sell": True,
        }

def get_signal(pair: str, current_price: float, multi_rsi: Dict[str, float] = None) -> Tuple[str, int, List[str]]:
    """Calculate trading signal based on F&G + RSI + daily position."""
    if multi_rsi is None:
        multi_rsi = {"3m": get_rsi(pair), "15m": 50.0, "1h": 50.0, "4h": 50.0, "1d": 50.0}
    
    rsi_3m = multi_rsi.get("3m", 50.0)
    rsi_1h = multi_rsi.get("1h", 50.0)
    fg = state.fg_value
    daily_pos = get_daily_position(pair, current_price)
    
    score = 0
    reasons = []
    
    if rsi_3m <= 30 and rsi_1h <= 40:
        return "STRONG_BUY", 10, [f"RSI_3M_OVERSOLD ({rsi_3m:.1f})", f"RSI_1H_OVERSOLD ({rsi_1h:.1f})", f"F&G ({fg})"]
    
    if fg <= FG_STRONG_BUY:
        fg_score = 3
        reasons.append(f"EXTREME_FEAR ({fg})")
    elif fg <= FG_BUY_THRESHOLD:
        fg_score = 2
        reasons.append(f"FEAR ({fg})")
    elif fg <= 50:
        fg_score = 0
        reasons.append(f"NEUTRAL ({fg})")
    elif fg >= FG_SELL_THRESHOLD:
        fg_score = -2
        reasons.append(f"GREED ({fg})")
    else:
        fg_score = -1
        reasons.append(f"{state.fg_class} ({fg})")
    score += fg_score
    
    if rsi_3m <= RSI_STRONG_BUY:
        rsi_score = 3
        reasons.append(f"RSI_OVERSOLD ({rsi_3m:.1f})")
    elif rsi_3m <= RSI_BUY_THRESHOLD:
        rsi_score = 2
        reasons.append(f"RSI_NEAR_OVERSOLD ({rsi_3m:.1f})")
    elif rsi_3m >= RSI_SELL_THRESHOLD:
        rsi_score = -2
        reasons.append(f"RSI_OVERBOUGHT ({rsi_3m:.1f})")
    elif rsi_3m >= 55:
        rsi_score = -1
        reasons.append(f"RSI_NEAR_OVERBOUGHT ({rsi_3m:.1f})")
    else:
        rsi_score = 0
        reasons.append(f"RSI_NEUTRAL ({rsi_3m:.1f})")
    score += rsi_score
    
    if daily_pos < DAILY_POS_BUY:
        pos_score = 2
        reasons.append(f"LOW_DAILY_POS ({daily_pos:.0f}%)")
    elif daily_pos > DAILY_POS_SELL:
        pos_score = -2
        reasons.append(f"HIGH_DAILY_POS ({daily_pos:.0f}%)")
    else:
        pos_score = 0
    score += pos_score
    
    if score >= 5:
        return "STRONG_BUY", score, reasons
    elif score >= 3:
        return "BUY", score, reasons
    elif score >= 1:
        return "WEAK_BUY", score, reasons
    elif score <= -3:
        return "STRONG_SELL", score, reasons
    elif score <= -1:
        return "SELL", score, reasons
    else:
        return "HOLD", score, reasons
