"""
Simplified RSI + Orderbook Trading Strategy V2

A streamlined trading strategy combining:
- RSI(14) for momentum
- Daily Position for range-based entries  
- Orderbook imbalance for institutional flow
- Price momentum direction

Uses only existing indicators that work without historical candles:
- get_rsi(pair) - current RSI value
- get_daily_position(pair, price) - 0-100% daily range
- get_orderbook(pair) - bid/ask imbalance

Source: Adapted from @calledkeyy strategy
Implementation: Hermes Trader
"""

import time
from typing import Optional, Dict, List
from dataclasses import dataclass, asdict

from hermes.logging_setup import log
from hermes.state import prices, state
from hermes.api.orderbook import get_orderbook, orderbook_confirms_signal
from hermes.config import (
    MAX_TRADE_USDT, MIN_TRADE_USDT, PAIR_DECIMAL_PLACES
)


# Strategy constants
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_OVERSOLD_EXIT = 35
RSI_OVERBOUGHT_EXIT = 65

# Daily position thresholds
DP_BUY_ZONE = 30  # Price in lower 30% of daily range
DP_SELL_ZONE = 70  # Price in upper 70% of daily range

# Orderbook imbalance thresholds
OB_BULLISH_THRESHOLD = 1.2   # bid_vol/ask_vol > 1.2 = bullish pressure
OB_BEARISH_THRESHOLD = 0.8   # bid_vol/ask_vol < 0.8 = bearish pressure

# Confidence levels
CONF_HIGH = "High"
CONF_MEDIUM = "Medium"
CONF_LOW = "Low"


@dataclass
class TradingSignal:
    """Complete trading signal with entry, exit, and metadata."""
    signal_type: str  # "LONG", "SHORT", "NO TRADE SETUP"
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    rsi_value: float
    daily_position: float
    trend_bias: str  # "bullish", "bearish", "neutral"
    signal_confidence: str  # "Low", "Medium", "High"
    reason: str
    orderbook_imbalance: float
    risk_percent: float
    position_size: float

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class StrategyV2:
    """Simplified RSI + Orderbook Trading Strategy.
    
    Replaces EMA crossover (which requires candles) with:
    - RSI momentum (using Wilder's smoothed RSI)
    - Daily Position (range-based entry timing)
    - Orderbook imbalance (institutional flow)
    - Price momentum (recent price changes)
    """
    
    def __init__(self, pair: str, capital: float = 0.0, risk_pct: float = 0.01):
        """Initialize strategy.
        
        Args:
            pair: Trading pair (e.g., 'doge', 'xrp')
            capital: Available capital in IDR
            risk_pct: Risk percentage per trade (0.01 = 1%)
        """
        self.pair = pair
        self.capital = capital if capital > 0 else MAX_TRADE_RP
        self.risk_pct = risk_pct
        
        # Track RSI history for direction
        self._rsi_history: List[float] = []
        self._max_rsi_history = 5  # Keep last 5 RSI values
    
    def analyze(self) -> TradingSignal:
        """Run complete strategy analysis.
        
        Returns:
            TradingSignal with all parameters
        """
        # Get current price
        current_price = self._get_current_price()
        if not current_price:
            return self._no_signal("No price data available")
        
        # Get RSI and track direction
        rsi = self._get_rsi_with_direction()
        rsi_value = rsi["current"]
        rsi_direction = rsi["direction"]  # "up", "down", "neutral"
        
        # Get daily position
        daily_pos = self._get_daily_position(current_price)
        
        # Get orderbook data
        ob = get_orderbook(self.pair)
        imbalance = ob.imbalance if ob else 1.0
        
        # Get recent price change for momentum
        price_change_pct = self._get_price_momentum()
        
        # Evaluate LONG/SHORT conditions
        signal, confidence, reasons = self._evaluate_signal(
            rsi_value=rsi_value,
            rsi_direction=rsi_direction,
            daily_pos=daily_pos,
            imbalance=imbalance,
            price_change_pct=price_change_pct
        )
        
        if signal == "NO TRADE SETUP":
            return self._no_signal(reasons[0] if reasons else "Signal conditions not met")
        
        # Calculate entry, stop loss, take profits
        entry_price, stop_loss, tp1, tp2, tp3 = self._calculate_entry_sl_tp(
            signal=signal,
            price=current_price,
            rsi_value=rsi_value,
            daily_pos=daily_pos
        )
        
        # Calculate position size
        position_size = self._calculate_position_size(
            entry_price=entry_price,
            stop_loss=stop_loss
        )
        
        # Determine trend bias
        trend_bias = self._determine_trend_bias(
            rsi_value=rsi_value,
            daily_pos=daily_pos,
            price_change_pct=price_change_pct
        )
        
        # Build reason string
        reason = self._build_reason(
            signal=signal,
            rsi_value=rsi_value,
            rsi_direction=rsi_direction,
            daily_pos=daily_pos,
            imbalance=imbalance,
            price_change_pct=price_change_pct,
            confidence=confidence,
            reasons=reasons
        )
        
        return TradingSignal(
            signal_type=signal,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rsi_value=rsi_value,
            daily_position=daily_pos,
            trend_bias=trend_bias,
            signal_confidence=confidence,
            reason=reason,
            orderbook_imbalance=imbalance,
            risk_percent=self.risk_pct * 100,
            position_size=position_size
        )
    
    def _get_current_price(self) -> Optional[float]:
        """Get current price from cache or REST."""
        # Check prices cache first
        cached = prices.get(self.pair, {})
        price = cached.get("price")
        if price and (time.time() - cached.get("ts", 0)) < 60:
            return float(price)
        
        # Fallback to REST and update cache
        from hermes.api.rest import fetch_price_rest, update_price
        price = fetch_price_rest(self.pair)
        if price:
            update_price(self.pair, price, source="strategy_v2")
        return price
    
    def _get_rsi_with_direction(self) -> Dict:
        """Get current RSI and calculate direction from history.
        
        Returns:
            Dict with 'current' RSI value and 'direction' (up/down/neutral)
        """
        from hermes.indicators.rsi import get_rsi, update_rsi
        
        current_price = self._get_current_price()
        if current_price:
            # Update RSI with current price
            rsi_value = update_rsi(self.pair, current_price, period=RSI_PERIOD)
        else:
            rsi_value = get_rsi(self.pair)
        
        # Track history for direction
        self._rsi_history.append(rsi_value)
        if len(self._rsi_history) > self._max_rsi_history:
            self._rsi_history.pop(0)
        
        # Calculate direction
        if len(self._rsi_history) >= 2:
            if self._rsi_history[-1] > self._rsi_history[-2]:
                direction = "up"
            elif self._rsi_history[-1] < self._rsi_history[-2]:
                direction = "down"
            else:
                direction = "neutral"
        else:
            direction = "neutral"
        
        return {"current": rsi_value, "direction": direction}
    
    def _get_daily_position(self, current_price: float) -> float:
        """Get daily position (0-100%) from existing function."""
        from hermes.indicators.signals import get_daily_position
        return get_daily_position(self.pair, current_price)
    
    def _get_price_momentum(self) -> float:
        """Calculate recent price change percentage.
        
        Returns:
            Price change in percent (e.g., 2.5 for 2.5% change)
        """
        history = state.price_history.get(self.pair, [])
        if len(history) >= 2:
            old_price = history[-2]
            if old_price > 0:
                return ((history[-1] - old_price) / old_price) * 100
        return 0.0
    
    def _evaluate_signal(
        self,
        rsi_value: float,
        rsi_direction: str,
        daily_pos: float,
        imbalance: float,
        price_change_pct: float
    ) -> tuple:
        """Evaluate LONG/SHORT/NO TRADE conditions.
        
        Returns:
            Tuple of (signal_type, confidence, reasons_list)
        """
        reasons = []
        conditions_met = 0
        total_conditions = 0
        
        # ===== LONG CONDITIONS =====
        long_conditions = []
        
        # 1. RSI in oversold or exiting
        total_conditions += 1
        if rsi_value <= RSI_OVERSOLD:
            long_conditions.append(("RSI_OVERSOLD", True, f"RSI {rsi_value:.1f} ≤ {RSI_OVERSOLD}"))
            conditions_met += 1
        elif rsi_value <= RSI_OVERSOLD_EXIT:
            long_conditions.append(("RSI_EXITING_OVERSOLD", True, f"RSI {rsi_value:.1f} near oversold"))
            conditions_met += 0.5  # Partial credit
        else:
            long_conditions.append(("RSI_NOT_OVERSOLD", False, f"RSI {rsi_value:.1f} above oversold"))
        
        # 2. Daily position in buy zone
        total_conditions += 1
        if daily_pos <= DP_BUY_ZONE:
            long_conditions.append(("DP_BUY_ZONE", True, f"Daily pos {daily_pos:.1f}% in buy zone"))
            conditions_met += 1
        elif daily_pos <= 40:
            long_conditions.append(("DP_NEAR_BUY", True, f"Daily pos {daily_pos:.1f}% near buy zone"))
            conditions_met += 0.5
        else:
            long_conditions.append(("DP_NOT_BUY", False, f"Daily pos {daily_pos:.1f}% above buy zone"))
        
        # 3. RSI direction upward (momentum confirmation)
        total_conditions += 1
        if rsi_direction == "up":
            long_conditions.append(("RSI_MOMENTUM_UP", True, "RSI momentum upward"))
            conditions_met += 1
        elif rsi_direction == "neutral":
            long_conditions.append(("RSI_MOMENTUM_NEUTRAL", False, "RSI momentum neutral"))
            conditions_met += 0.3
        else:
            long_conditions.append(("RSI_MOMENTUM_DOWN", False, "RSI momentum downward"))
        
        # 4. Orderbook bullish
        total_conditions += 1
        if imbalance >= OB_BULLISH_THRESHOLD:
            long_conditions.append(("OB_BULLISH", True, f"Orderbook bullish {imbalance:.2f}x"))
            conditions_met += 1
        elif imbalance >= 1.0:
            long_conditions.append(("OB_NEUTRAL_BULL", True, f"Orderbook slight bullish {imbalance:.2f}x"))
            conditions_met += 0.5
        else:
            long_conditions.append(("OB_NOT_BULLISH", False, f"Orderbook not bullish {imbalance:.2f}x"))
        
        # 5. Positive price momentum
        total_conditions += 1
        if price_change_pct > 0:
            long_conditions.append(("PRICE_UP", True, f"Price +{price_change_pct:.2f}%"))
            conditions_met += 1
        elif price_change_pct > -0.5:
            long_conditions.append(("PRICE_FLAT", False, f"Price {price_change_pct:.2f}%"))
            conditions_met += 0.3
        else:
            long_conditions.append(("PRICE_DOWN", False, f"Price {price_change_pct:.2f}%"))
        
        # Check LONG conditions
        for name, met, desc in long_conditions:
            if met:
                reasons.append(desc)
        
        # Determine signal
        long_score = conditions_met / total_conditions
        
        # LONG check
        if (rsi_value <= RSI_OVERSOLD_EXIT and 
            daily_pos <= DP_BUY_ZONE and 
            rsi_direction in ["up", "neutral"]):
            
            if long_score >= 0.8 and imbalance >= 1.0:
                confidence = CONF_HIGH
            elif long_score >= 0.6:
                confidence = CONF_MEDIUM
            else:
                confidence = CONF_LOW
            
            return "LONG", confidence, reasons
        
        # ===== SHORT CONDITIONS =====
        reasons = []
        conditions_met = 0
        total_conditions = 0
        
        short_conditions = []
        
        # 1. RSI in overbought or exiting
        total_conditions += 1
        if rsi_value >= RSI_OVERBOUGHT:
            short_conditions.append(("RSI_OVERBOUGHT", True, f"RSI {rsi_value:.1f} ≥ {RSI_OVERBOUGHT}"))
            conditions_met += 1
        elif rsi_value >= RSI_OVERBOUGHT_EXIT:
            short_conditions.append(("RSI_EXITING_OVERBOUGHT", True, f"RSI {rsi_value:.1f} near overbought"))
            conditions_met += 0.5
        else:
            short_conditions.append(("RSI_NOT_OVERBOUGHT", False, f"RSI {rsi_value:.1f} below overbought"))
        
        # 2. Daily position in sell zone
        total_conditions += 1
        if daily_pos >= DP_SELL_ZONE:
            short_conditions.append(("DP_SELL_ZONE", True, f"Daily pos {daily_pos:.1f}% in sell zone"))
            conditions_met += 1
        elif daily_pos >= 60:
            short_conditions.append(("DP_NEAR_SELL", True, f"Daily pos {daily_pos:.1f}% near sell zone"))
            conditions_met += 0.5
        else:
            short_conditions.append(("DP_NOT_SELL", False, f"Daily pos {daily_pos:.1f}% below sell zone"))
        
        # 3. RSI direction downward
        total_conditions += 1
        if rsi_direction == "down":
            short_conditions.append(("RSI_MOMENTUM_DOWN", True, "RSI momentum downward"))
            conditions_met += 1
        elif rsi_direction == "neutral":
            short_conditions.append(("RSI_MOMENTUM_NEUTRAL", False, "RSI momentum neutral"))
            conditions_met += 0.3
        else:
            short_conditions.append(("RSI_MOMENTUM_UP", False, "RSI momentum upward"))
        
        # 4. Orderbook bearish
        total_conditions += 1
        if imbalance <= OB_BEARISH_THRESHOLD:
            short_conditions.append(("OB_BEARISH", True, f"Orderbook bearish {imbalance:.2f}x"))
            conditions_met += 1
        elif imbalance <= 1.0:
            short_conditions.append(("OB_NEUTRAL_BEAR", True, f"Orderbook slight bearish {imbalance:.2f}x"))
            conditions_met += 0.5
        else:
            short_conditions.append(("OB_NOT_BEARISH", False, f"Orderbook not bearish {imbalance:.2f}x"))
        
        # 5. Negative price momentum
        total_conditions += 1
        if price_change_pct < 0:
            short_conditions.append(("PRICE_DOWN", True, f"Price {price_change_pct:.2f}%"))
            conditions_met += 1
        elif price_change_pct < 0.5:
            short_conditions.append(("PRICE_FLAT", False, f"Price {price_change_pct:.2f}%"))
            conditions_met += 0.3
        else:
            short_conditions.append(("PRICE_UP", False, f"Price +{price_change_pct:.2f}%"))
        
        for name, met, desc in short_conditions:
            if met:
                reasons.append(desc)
        
        short_score = conditions_met / total_conditions
        
        if (rsi_value >= RSI_OVERBOUGHT_EXIT and 
            daily_pos >= DP_SELL_ZONE and 
            rsi_direction in ["down", "neutral"]):
            
            if short_score >= 0.8 and imbalance <= 1.0:
                confidence = CONF_HIGH
            elif short_score >= 0.6:
                confidence = CONF_MEDIUM
            else:
                confidence = CONF_LOW
            
            return "SHORT", confidence, reasons
        
        return "NO TRADE SETUP", CONF_LOW, ["Conditions not aligned"]
    
    def _calculate_entry_sl_tp(
        self,
        signal: str,
        price: float,
        rsi_value: float,
        daily_pos: float
    ) -> tuple:
        """Calculate entry price, stop loss, and take profit levels.
        
        Returns:
            Tuple of (entry_price, stop_loss, tp1, tp2, tp3)
        """
        # For LONG: SL below entry, TP above entry
        # For SHORT: SL above entry, TP below entry
        
        if signal == "LONG":
            # Stop loss: 2-3% below entry, wider if RSI deeply oversold
            if rsi_value <= RSI_OVERSOLD:
                sl_pct = 0.03  # 3% for very oversold
            else:
                sl_pct = 0.02  # 2% normal
            
            stop_loss = price * (1 - sl_pct)
            entry_price = price
            
            # Take profits at 1:1, 1:2, 1:3 R:R
            risk = entry_price - stop_loss
            tp1 = entry_price + risk * 1
            tp2 = entry_price + risk * 2
            tp3 = entry_price + risk * 3
            
        elif signal == "SHORT":
            if rsi_value >= RSI_OVERBOUGHT:
                sl_pct = 0.03
            else:
                sl_pct = 0.02
            
            stop_loss = price * (1 + sl_pct)
            entry_price = price
            
            risk = stop_loss - entry_price
            tp1 = entry_price - risk * 1
            tp2 = entry_price - risk * 2
            tp3 = entry_price - risk * 3
        else:
            stop_loss = 0
            tp1 = tp2 = tp3 = 0
        
        return entry_price, stop_loss, tp1, tp2, tp3
    
    def _calculate_position_size(self, entry_price: float, stop_loss: float) -> float:
        """Calculate position size based on risk.
        
        Returns:
            Position size in coin quantity
        """
        if entry_price <= 0 or stop_loss <= 0:
            return 0.0
        
        risk_amount = self.capital * self.risk_pct
        risk_per_unit = abs(entry_price - stop_loss)
        
        if risk_per_unit == 0:
            return 0.0
        
        position_size = risk_amount / risk_per_unit
        return position_size
    
    def _determine_trend_bias(
        self,
        rsi_value: float,
        daily_pos: float,
        price_change_pct: float
    ) -> str:
        """Determine overall trend bias."""
        bullish_signals = 0
        bearish_signals = 0
        
        # RSI
        if rsi_value < 45:
            bullish_signals += 1
        elif rsi_value > 55:
            bearish_signals += 1
        
        # Daily position
        if daily_pos < 45:
            bullish_signals += 1
        elif daily_pos > 55:
            bearish_signals += 1
        
        # Price momentum
        if price_change_pct > 0.5:
            bullish_signals += 1
        elif price_change_pct < -0.5:
            bearish_signals += 1
        
        if bullish_signals >= 2 and bearish_signals == 0:
            return "bullish"
        elif bearish_signals >= 2 and bullish_signals == 0:
            return "bearish"
        return "neutral"
    
    def _build_reason(
        self,
        signal: str,
        rsi_value: float,
        rsi_direction: str,
        daily_pos: float,
        imbalance: float,
        price_change_pct: float,
        confidence: str,
        reasons: List[str]
    ) -> str:
        """Build human-readable reason string."""
        base = f"{signal} signal ({confidence} confidence)"
        
        details = []
        details.append(f"RSI {rsi_value:.1f} ({rsi_direction})")
        details.append(f"Daily pos {daily_pos:.1f}%")
        details.append(f"Orderbook {imbalance:.2f}x")
        
        return f"{base} — {', '.join(details)}"
    
    def _no_signal(self, reason: str) -> TradingSignal:
        """Return a no-trade signal."""
        return TradingSignal(
            signal_type="NO TRADE SETUP",
            entry_price=0.0,
            stop_loss=0.0,
            take_profit_1=0.0,
            take_profit_2=0.0,
            take_profit_3=0.0,
            rsi_value=50.0,
            daily_position=50.0,
            trend_bias="neutral",
            signal_confidence=CONF_LOW,
            reason=reason,
            orderbook_imbalance=1.0,
            risk_percent=self.risk_pct * 100,
            position_size=0.0
        )


def get_signal_v2(pair: str, capital: float = 0.0, risk_pct: float = 0.01) -> dict:
    """Convenience function for CLI.
    
    Returns dict (JSON-serializable) instead of TradingSignal object.
    """
    strategy = StrategyV2(pair=pair, capital=capital, risk_pct=risk_pct)
    signal = strategy.analyze()
    return {"pair": pair, "signal": signal.to_dict()}


async def get_signal_v2_async(pair: str, capital: float = 0.0, risk_pct: float = 0.01) -> dict:
    """Async convenience function for daemon."""
    return get_signal_v2(pair, capital, risk_pct)


def analyze_pair_v2(pair: str, capital: float = 0.0) -> TradingSignal:
    """Standalone analysis function for display/CLI."""
    strategy = StrategyV2(pair=pair, capital=capital)
    return strategy.analyze()
