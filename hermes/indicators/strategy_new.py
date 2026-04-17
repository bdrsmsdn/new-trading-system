"""
RSI + EMA Crossover + Orderbook Trading Strategy

A professional trading strategy combining:
- RSI(14) for momentum confirmation
- EMA(9,21) crossover for trend detection
- Orderbook imbalance for institutional flow analysis
- Multi-timeframe confirmation (5m and 15m)

Author: @calledkeyy (strategy source)
Implementation: Hermes Trader
"""

import time
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass, asdict
from hermes.logging_setup import log
from hermes.state import prices, state
from hermes.api.rest import fetch_price_rest, _check_budget
from hermes.api.orderbook import get_orderbook, OrderbookData, orderbook_confirms_signal
from hermes.indicators.candles import (
    calc_ema, calc_ema_from_candles, get_candle_closes,
    get_swing_low, get_swing_high, is_low_volatility,
    detect_ema_crossover_history, get_candle_data
)
from hermes.indicators.rsi import calc_rsi_from_candles
from hermes.config import (
    MAX_TRADE_RP, MIN_TRADE_RP, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    PAIR_DECIMAL_PLACES
)


# Strategy constants
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_OVERSOLD_EXIT = 35  # RSI exiting oversold zone
RSI_OVERBOUGHT_EXIT = 65  # RSI exiting overbought zone
MAX_CROSSOVER_AGE = 3  # Ignore signals if crossover > 3 candles ago
LOW_VOLATILITY_THRESHOLD = 0.003  # 0.3% ATR threshold

# Signal confidence levels
CONFIDENCE_HIGH = "High"
CONFIDENCE_MEDIUM = "Medium"
CONFIDENCE_LOW = "Low"


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
    ema_9: float
    ema_21: float
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
    """RSI + EMA Crossover + Orderbook Trading Strategy."""
    
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
        self.intervals = ["5m", "15m"]  # Multi-timeframe analysis
    
    def analyze(self) -> TradingSignal:
        """Run complete strategy analysis.
        
        Returns:
            TradingSignal with all parameters
        """
        # Get current price
        current_price = self._get_current_price()
        if not current_price:
            return self._no_signal("No price data available")
        
        # Get orderbook data
        ob = get_orderbook(self.pair)
        imbalance = ob.imbalance if ob else 1.0
        
        # Get candle data for multiple timeframes
        tf_data = self._get_multi_timeframe_data()
        if not tf_data:
            return self._no_signal("No candle data available")
        
        # Use 5m as primary, 15m for confirmation
        primary = tf_data.get("5m", {})
        secondary = tf_data.get("15m", {})
        
        if not primary.get("ema9") or not primary.get("ema21"):
            return self._no_signal("Insufficient EMA data")
        
        # Extract values
        ema9 = primary["ema9"]
        ema21 = primary["ema21"]
        ema9_prev = primary.get("ema9_prev", ema9)
        ema21_prev = primary.get("ema21_prev", ema21)
        rsi = primary.get("rsi")
        candles = primary.get("candles", [])
        current_price_tf = primary.get("current_price", current_price)
        low_volatility = primary.get("low_volatility", False)
        
        # Check signal filters
        if low_volatility:
            return self._no_signal("Low volatility - avoiding signal")
        
        # Detect EMA crossover
        crossover_type, crossover_age = detect_ema_crossover_history(
            primary.get("ema9_history", [ema9, ema9_prev]),
            primary.get("ema21_history", [ema21, ema21_prev])
        )
        
        if crossover_age is not None and crossover_age > MAX_CROSSOVER_AGE:
            return self._no_signal(f"EMA crossover too old ({crossover_age} candles ago)")
        
        # Calculate RSI direction
        rsi_direction = self._get_rsi_direction(candles)
        
        # Determine signal
        signal = self._evaluate_signal(
            crossover_type=crossover_type,
            rsi=rsi,
            rsi_direction=rsi_direction,
            price=current_price_tf,
            ema9=ema9,
            ema21=ema21,
            ob=ob,
            secondary=secondary
        )
        
        if signal == "NO TRADE SETUP":
            return self._no_signal("Signal conditions not met")
        
        # Calculate entry, stop loss, take profits
        entry_price, stop_loss, tp1, tp2, tp3 = self._calculate_entry_sl_tp(
            signal=signal,
            price=current_price_tf,
            candles=candles,
            ema9=ema9,
            ema21=ema21
        )
        
        # Calculate position size
        risk_amount = self.capital * self.risk_pct
        risk_per_unit = abs(entry_price - stop_loss) if stop_loss else entry_price * 0.02
        position_size = risk_amount / risk_per_unit if risk_per_unit > 0 else 0
        
        # Determine confidence
        confidence = self._calculate_confidence(
            signal=signal,
            rsi=rsi,
            crossover_age=crossover_age,
            ob=ob,
            secondary=secondary
        )
        
        # Determine trend bias
        trend_bias = self._determine_trend_bias(
            ema9=ema9,
            ema21=ema21,
            price=current_price_tf
        )
        
        # Build reason string
        reason = self._build_reason(
            signal=signal,
            rsi=rsi,
            crossover_type=crossover_type,
            crossover_age=crossover_age,
            ob=ob,
            confidence=confidence
        )
        
        return TradingSignal(
            signal_type=signal,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rsi_value=rsi if rsi else 50.0,
            ema_9=ema9,
            ema_21=ema21,
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
            return price
        
        # Fallback to REST (with budget check)
        if _check_budget():
            return fetch_price_rest(self.pair)
        
        return price
    
    def _get_multi_timeframe_data(self) -> Dict[str, dict]:
        """Get candle data for multiple timeframes.
        
        Returns:
            Dict mapping interval -> {
                'candles': [...],
                'ema9': float,
                'ema21': float,
                'rsi': float,
                'ema9_prev': float,
                'ema21_prev': float,
                'current_price': float,
                'low_volatility': bool
            }
        """
        result = {}
        
        for interval in self.intervals:
            candles = get_candle_data(self.pair, interval=interval, limit=100)
            
            if not candles or len(candles) < EMA_SLOW + 1:
                continue
            
            closes = get_candle_closes(candles)
            
            # Calculate current and previous EMAs
            ema9 = calc_ema(closes, EMA_FAST)
            ema21 = calc_ema(closes, EMA_SLOW)
            
            # Previous candle's EMA (need 2 candles back for crossover detection)
            if len(closes) >= EMA_SLOW + 2:
                closes_with_prev = closes[:-1]  # Remove current
                ema9_prev = calc_ema(closes_with_prev, EMA_FAST)
                ema21_prev = calc_ema(closes_with_prev, EMA_SLOW)
            else:
                ema9_prev = ema9
                ema21_prev = ema21
            
            # EMA history for crossover detection
            ema9_history = []
            ema21_history = []
            for i in range(min(5, len(closes))):
                subset = closes[:-(i) if i > 0 else None] if i > 0 else closes
                if len(subset) >= EMA_SLOW:
                    ema9_history.append(calc_ema(subset, EMA_FAST))
                    ema21_history.append(calc_ema(subset, EMA_SLOW))
            
            # Calculate RSI
            rsi = calc_rsi_from_candles(candles, period=RSI_PERIOD)
            
            # Check volatility
            low_vol = is_low_volatility(candles, threshold_pct=LOW_VOLATILITY_THRESHOLD)
            
            result[interval] = {
                "candles": candles,
                "ema9": ema9,
                "ema21": ema21,
                "ema9_prev": ema9_prev,
                "ema21_prev": ema21_prev,
                "ema9_history": list(reversed(ema9_history)) if ema9_history else [ema9],
                "ema21_history": list(reversed(ema21_history)) if ema21_history else [ema21],
                "rsi": rsi,
                "current_price": closes[-1] if closes else None,
                "low_volatility": low_vol
            }
        
        return result
    
    def _get_rsi_direction(self, candles: List[List[float]]) -> str:
        """Determine RSI direction from recent candles.
        
        Args:
            candles: Recent candles
        
        Returns:
            'UP', 'DOWN', or 'FLAT'
        """
        if len(candles) < 5:
            return "FLAT"
        
        # Calculate RSI for last 5 candles
        rsi_values = []
        for i in range(1, 6):
            if len(candles) >= i + RSI_PERIOD:
                subset = candles[:-(i-1)] if i > 1 else candles
                rsi = calc_rsi_from_candles(subset, period=RSI_PERIOD)
                if rsi is not None:
                    rsi_values.append(rsi)
        
        if len(rsi_values) < 2:
            return "FLAT"
        
        # Check direction: compare oldest to newest in our window
        if rsi_values[-1] > rsi_values[0] + 2:
            return "UP"
        elif rsi_values[-1] < rsi_values[0] - 2:
            return "DOWN"
        
        return "FLAT"
    
    def _evaluate_signal(
        self,
        crossover_type: str,
        rsi: Optional[float],
        rsi_direction: str,
        price: float,
        ema9: float,
        ema21: float,
        ob: Optional[OrderbookData],
        secondary: dict
    ) -> str:
        """Evaluate if a trade signal is present.
        
        Args:
            crossover_type: "BULLISH", "BEARISH", or "NONE"
            rsi: Current RSI value
            rsi_direction: "UP", "DOWN", or "FLAT"
            price: Current price
            ema9: Current EMA9
            ema21: Current EMA21
            ob: Orderbook data
            secondary: Secondary timeframe data
        
        Returns:
            "LONG", "SHORT", or "NO TRADE SETUP"
        """
        # LONG SETUP: All conditions must be true
        long_conditions = []
        
        # 1. RSI <= 30 OR RSI exiting oversold
        if rsi is not None:
            rsi_ok = rsi <= RSI_OVERSOLD or (rsi <= RSI_OVERSOLD_EXIT and rsi_direction == "UP")
            long_conditions.append(("RSI oversold condition", rsi_ok))
        else:
            long_conditions.append(("RSI oversold condition", False))
        
        # 2. EMA 9 crosses ABOVE EMA 21 (bullish crossover)
        crossover_ok = crossover_type == "BULLISH"
        long_conditions.append(("Bullish EMA crossover", crossover_ok))
        
        # 3. Price closes above both EMAs
        price_above_emas = price > ema9 and price > ema21
        long_conditions.append(("Price above EMAs", price_above_emas))
        
        # 4. RSI moving upward (confirming momentum)
        rsi_momentum_ok = rsi_direction == "UP"
        long_conditions.append(("RSI upward momentum", rsi_momentum_ok))
        
        # Check LONG conditions
        if all(condition[1] for condition in long_conditions):
            # Optional: Check orderbook confirmation for LONG
            if ob and not orderbook_confirms_signal(ob, "LONG"):
                log.debug(f"[STRATEGY-V2] {self.pair.upper()}: LONG signal but orderbook not confirming")
            
            # Cross-check with secondary timeframe (15m)
            if secondary:
                sec_ema9 = secondary.get("ema9")
                sec_ema21 = secondary.get("ema21")
                sec_price = secondary.get("current_price")
                if sec_ema9 and sec_ema21 and sec_price:
                    # If 15m also bullish, it's a stronger signal
                    if sec_price > sec_ema9 > sec_ema21:
                        log.debug(f"[STRATEGY-V2] {self.pair.upper()}: LONG confirmed by 15m timeframe")
            
            return "LONG"
        
        # SHORT SETUP: All conditions must be true
        short_conditions = []
        
        # 1. RSI >= 70 OR RSI exiting overbought
        if rsi is not None:
            rsi_ok = rsi >= RSI_OVERBOUGHT or (rsi >= RSI_OVERBOUGHT_EXIT and rsi_direction == "DOWN")
            short_conditions.append(("RSI overbought condition", rsi_ok))
        else:
            short_conditions.append(("RSI overbought condition", False))
        
        # 2. EMA 9 crosses BELOW EMA 21 (bearish crossover)
        crossover_ok = crossover_type == "BEARISH"
        short_conditions.append(("Bearish EMA crossover", crossover_ok))
        
        # 3. Price closes below both EMAs
        price_below_emas = price < ema9 and price < ema21
        short_conditions.append(("Price below EMAs", price_below_emas))
        
        # 4. RSI moving downward (confirming momentum)
        rsi_momentum_ok = rsi_direction == "DOWN"
        short_conditions.append(("RSI downward momentum", rsi_momentum_ok))
        
        # Check SHORT conditions
        if all(condition[1] for condition in short_conditions):
            if ob and not orderbook_confirms_signal(ob, "SHORT"):
                log.debug(f"[STRATEGY-V2] {self.pair.upper()}: SHORT signal but orderbook not confirming")
            
            # Cross-check with secondary timeframe
            if secondary:
                sec_ema9 = secondary.get("ema9")
                sec_ema21 = secondary.get("ema21")
                sec_price = secondary.get("current_price")
                if sec_ema9 and sec_ema21 and sec_price:
                    if sec_price < sec_ema9 < sec_ema21:
                        log.debug(f"[STRATEGY-V2] {self.pair.upper()}: SHORT confirmed by 15m timeframe")
            
            return "SHORT"
        
        return "NO TRADE SETUP"
    
    def _calculate_entry_sl_tp(
        self,
        signal: str,
        price: float,
        candles: List[List[float]],
        ema9: float,
        ema21: float
    ) -> Tuple[float, float, float, float, float]:
        """Calculate entry price, stop loss, and take profit levels.
        
        Args:
            signal: "LONG" or "SHORT"
            price: Current price
            candles: Recent candles
            ema9: Current EMA9
            ema21: Current EMA21
        
        Returns:
            Tuple of (entry_price, stop_loss, tp1, tp2, tp3)
        """
        # Entry at current price (or slightly better)
        entry_price = price
        
        if signal == "LONG":
            # Stop loss: below recent swing low
            swing_low = get_swing_low(candles, lookback=10)
            if swing_low and swing_low < price * 0.98:
                stop_loss = swing_low * 0.999  # Just below swing low
            else:
                # Fallback: ATR-based stop
                stop_loss = price * (1 - STOP_LOSS_PCT)
            
            # Take profits based on risk:reward
            risk = entry_price - stop_loss
            tp1 = entry_price + risk * 1.0  # 1:1
            tp2 = entry_price + risk * 2.0  # 1:2
            tp3 = entry_price + risk * 3.0  # 1:3
        
        elif signal == "SHORT":
            # Stop loss: above recent swing high
            swing_high = get_swing_high(candles, lookback=10)
            if swing_high and swing_high > price * 1.02:
                stop_loss = swing_high * 1.001  # Just above swing high
            else:
                stop_loss = price * (1 + STOP_LOSS_PCT)
            
            # Take profits
            risk = stop_loss - entry_price
            tp1 = entry_price - risk * 1.0  # 1:1
            tp2 = entry_price - risk * 2.0  # 1:2
            tp3 = entry_price - risk * 3.0  # 1:3
        
        else:
            stop_loss = 0
            tp1 = tp2 = tp3 = 0
        
        return entry_price, stop_loss, tp1, tp2, tp3
    
    def _calculate_confidence(
        self,
        signal: str,
        rsi: Optional[float],
        crossover_age: Optional[int],
        ob: Optional[OrderbookData],
        secondary: dict
    ) -> str:
        """Calculate signal confidence level.
        
        Args:
            signal: "LONG" or "SHORT"
            rsi: Current RSI
            crossover_age: Candles since crossover
            ob: Orderbook data
            secondary: Secondary timeframe data
        
        Returns:
            "Low", "Medium", or "High"
        """
        score = 0
        
        # RSI at extreme (oversold/overbought) = +1
        if signal == "LONG" and rsi and rsi <= RSI_OVERSOLD:
            score += 1
        elif signal == "SHORT" and rsi and rsi >= RSI_OVERBOUGHT:
            score += 1
        
        # Fresh crossover (1 candle ago) = +1
        if crossover_age == 1:
            score += 1
        elif crossover_age == 2:
            score += 0.5
        
        # Orderbook confirms = +1
        if ob and orderbook_confirms_signal(ob, signal):
            score += 1
        
        # Secondary timeframe confirms = +1
        if secondary:
            sec_rsi = secondary.get("rsi")
            if signal == "LONG" and sec_rsi and sec_rsi <= RSI_OVERSOLD_EXIT:
                score += 1
            elif signal == "SHORT" and sec_rsi and sec_rsi >= RSI_OVERBOUGHT_EXIT:
                score += 1
        
        if score >= 3:
            return CONFIDENCE_HIGH
        elif score >= 2:
            return CONFIDENCE_MEDIUM
        else:
            return CONFIDENCE_LOW
    
    def _determine_trend_bias(
        self,
        ema9: float,
        ema21: float,
        price: float
    ) -> str:
        """Determine trend bias from EMA relationship.
        
        Args:
            ema9: Current EMA9
            ema21: Current EMA21
            price: Current price
        
        Returns:
            "bullish", "bearish", or "neutral"
        """
        if price > ema9 > ema21:
            return "bullish"
        elif price < ema9 < ema21:
            return "bearish"
        elif price > ema9:
            return "bullish"
        elif price < ema9:
            return "bearish"
        else:
            return "neutral"
    
    def _build_reason(
        self,
        signal: str,
        rsi: Optional[float],
        crossover_type: str,
        crossover_age: Optional[int],
        ob: Optional[OrderbookData],
        confidence: str
    ) -> str:
        """Build human-readable reason for the signal.
        
        Args:
            signal: "LONG", "SHORT", or "NO TRADE SETUP"
            rsi: Current RSI
            crossover_type: "BULLISH", "BEARISH", or "NONE"
            crossover_age: Candles since crossover
            ob: Orderbook data
            confidence: Signal confidence
        
        Returns:
            Human-readable reason string
        """
        reasons = []
        
        if signal == "LONG":
            reasons.append("LONG setup")
            if rsi:
                if rsi <= RSI_OVERSOLD:
                    reasons.append(f"RSI deeply oversold ({rsi:.1f})")
                else:
                    reasons.append(f"RSI exiting oversold ({rsi:.1f})")
            reasons.append(f"EMA {crossover_type} crossover")
            if crossover_age:
                reasons.append(f"crossover {crossover_age} candle(s) ago")
            if ob:
                if ob.imbalance > 1.3:
                    reasons.append(f"thick bids (imbalance: {ob.imbalance:.2f})")
            reasons.append(f"{confidence} confidence")
        
        elif signal == "SHORT":
            reasons.append("SHORT setup")
            if rsi:
                if rsi >= RSI_OVERBOUGHT:
                    reasons.append(f"RSI deeply overbought ({rsi:.1f})")
                else:
                    reasons.append(f"RSI exiting overbought ({rsi:.1f})")
            reasons.append(f"EMA {crossover_type} crossover")
            if crossover_age:
                reasons.append(f"crossover {crossover_age} candle(s) ago")
            if ob:
                if ob.imbalance < 0.7:
                    reasons.append(f"thick asks (imbalance: {ob.imbalance:.2f})")
            reasons.append(f"{confidence} confidence")
        
        else:
            reasons.append("No trade setup - conditions not aligned")
        
        return "; ".join(reasons)
    
    def _no_signal(self, reason: str) -> TradingSignal:
        """Create a no-signal result.
        
        Args:
            reason: Why no signal
        
        Returns:
            TradingSignal with NO TRADE SETUP
        """
        return TradingSignal(
            signal_type="NO TRADE SETUP",
            entry_price=0.0,
            stop_loss=0.0,
            take_profit_1=0.0,
            take_profit_2=0.0,
            take_profit_3=0.0,
            rsi_value=50.0,
            ema_9=0.0,
            ema_21=0.0,
            trend_bias="neutral",
            signal_confidence="Low",
            reason=reason,
            orderbook_imbalance=1.0,
            risk_percent=self.risk_pct * 100,
            position_size=0.0
        )


# ── Convenience functions ──

def get_signal_v2(pair: str, capital: float = 0.0, risk_pct: float = 0.01) -> dict:
    """Get trading signal for a pair using StrategyV2.
    
    Args:
        pair: Trading pair (e.g., 'doge', 'xrp')
        capital: Available capital in IDR (uses default if not provided)
        risk_pct: Risk percentage (default 1%)
    
    Returns:
        Dict with signal data (compatible with output format)
    """
    strategy = StrategyV2(pair=pair, capital=capital, risk_pct=risk_pct)
    signal = strategy.analyze()
    return signal.to_dict()


async def get_signal_v2_async(pair: str, capital: float = 0.0, risk_pct: float = 0.01) -> dict:
    """Async version of get_signal_v2.
    
    Note: Most of the work is CPU-bound, so runs in thread pool.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_signal_v2, pair, capital, risk_pct)


def analyze_pair_v2(pair: str, capital: float = 0.0) -> dict:
    """Analyze a pair and return complete analysis.
    
    Args:
        pair: Trading pair
        capital: Available capital
    
    Returns:
        Dict with signal + orderbook + metadata
    """
    signal = get_signal_v2(pair, capital)
    ob = get_orderbook(pair)
    
    return {
        "pair": pair,
        "signal": signal,
        "orderbook": {
            "imbalance": ob.imbalance if ob else 1.0,
            "bid_volume": ob.bid_volume if ob else 0.0,
            "ask_volume": ob.ask_volume if ob else 0.0,
            "spread": ob.spread if ob else 0.0,
            "thick_bid": ob.thick_bid_level if ob else None,
            "thick_ask": ob.thick_ask_level if ob else None,
        } if ob else None
    }
