"""
Backtesting engine for the Hermes trading system.

Simulates trades on historical candle data using Strategy V2 signals
or custom signal lists, and computes detailed performance statistics.
"""

import math
from typing import List, Dict, Optional
from dataclasses import dataclass

from hermes.api.rest import fetch_candles
from hermes.indicators.rsi import calc_rsi_from_candles
from hermes.logging_setup import log

# Default risk per trade (1%)
DEFAULT_RISK_PCT = 0.01


@dataclass
class Trade:
    """Represents a single completed trade."""
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    side: str          # "LONG" or "SHORT"
    qty: float
    pnl: float         # absolute IDR PnL
    pnl_pct: float     # percentage gain/loss
    hold_hours: float


class Backtester:
    """Backtesting engine for historical strategy simulation."""

    def __init__(self, initial_capital: float = 1_000_000):
        """Initialize backtester.

        Args:
            initial_capital: Starting capital in IDR (default 1_000_000)
        """
        self.initial_capital = initial_capital

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, pair: str, days: int = 30, interval: str = "1h") -> Dict:
        """Run backtest for a pair using Strategy V2 signals.

        Args:
            pair: Trading pair (e.g., 'doge')
            days: Number of days of historical data
            interval: Candle timeframe (e.g., '1h', '4h', '1d')

        Returns:
            Dict with equity_curve, trades, and stats keys.
        """
        limit = self._days_to_candles(days, interval)
        candles = fetch_candles(pair, interval=interval, limit=limit)
        if not candles or len(candles) < 20:
            log.warning(f"[Backtester] Insufficient candle data for {pair} ({interval})")
            return self._empty_result()

        # Build signal list from Strategy V2 adapted for backtesting
        signals = self._generate_signals_v2(pair, candles)
        return self.run_signal_backtest(pair, signals, days=days)

    def run_signal_backtest(
        self,
        pair: str,
        signals: List[Dict],
        days: int = 30
    ) -> Dict:
        """Run backtest given a list of signal dicts.

        Each signal dict must have:
            time   — candle timestamp (float)
            signal — "LONG", "SHORT", or "EXIT"
            price  — close price at signal time (float)

        Args:
            pair: Trading pair
            signals: List of signal dicts
            days: Number of backtest days (used for info only)

        Returns:
            Dict with equity_curve, trades, and stats.
        """
        if not signals:
            return self._empty_result()

        # Sort signals by time
        signals = sorted(signals, key=lambda s: s["time"])

        equity = self.initial_capital
        peak = equity
        max_drawdown = 0.0
        equity_curve: List[Dict] = []
        trades: List[Trade] = []
        position: Optional[Dict] = None

        # Iterate through signals chronologically
        for sig in signals:
            ts = sig["time"]
            sig_type = sig["signal"]
            price = sig["price"]

            # Record equity at this point
            equity_curve.append({"time": ts, "equity": equity})

            if sig_type == "EXIT":
                if position:
                    trade = self._close_trade(position, ts, price, equity)
                    trades.append(trade)
                    equity += trade.pnl
                    position = None

            elif sig_type == "LONG":
                if position:
                    if position["side"] == "SHORT":
                        trade = self._close_trade(position, ts, price, equity)
                        trades.append(trade)
                        equity += trade.pnl
                # Open LONG (or keep existing LONG if already open)
                if not position or position["side"] != "LONG":
                    position = {
                        "side": "LONG",
                        "entry_time": ts,
                        "entry_price": price,
                        "qty": 0,
                        "risk_amount": equity * DEFAULT_RISK_PCT,
                    }

            elif sig_type == "SHORT":
                if position:
                    if position["side"] == "LONG":
                        trade = self._close_trade(position, ts, price, equity)
                        trades.append(trade)
                        equity += trade.pnl
                # Open SHORT (or keep existing SHORT if already open)
                if not position or position["side"] != "SHORT":
                    position = {
                        "side": "SHORT",
                        "entry_time": ts,
                        "entry_price": price,
                        "qty": 0,
                        "risk_amount": equity * DEFAULT_RISK_PCT,
                    }

            # Update peak and drawdown
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_drawdown:
                max_drawdown = dd

        # Close any open position at the last known price
        if position:
            last_sig = signals[-1]
            trade = self._close_trade(position, last_sig["time"], last_sig["price"], equity)
            trades.append(trade)
            equity += trade.pnl
            position = None

        # Final equity point
        equity_curve.append({"time": signals[-1]["time"], "equity": equity})

        # Compute stats
        stats = self._compute_stats(trades, equity, max_drawdown)

        return {
            "equity_curve": equity_curve,
            "trades": [self._trade_to_dict(t) for t in trades],
            **stats,
        }

    # ------------------------------------------------------------------
    # Strategy V2 signal generation (backtest-adapted, no confirmation)
    # ------------------------------------------------------------------

    def _generate_signals_v2(
        self,
        pair: str,
        candles: List[List[float]]
    ) -> List[Dict]:
        """Generate Strategy V2 signals from historical candles.

        This is an adapted version of get_signal_v2() that:
          - Operates purely on historical close prices (no live state)
          - Does not require confirmation — auto-executes on signal
        """
        signals: List[Dict] = []
        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        timestamps = [float(c[0]) for c in candles]

        rsi_history: List[float] = []
        rsi_period = 14
        lookback = 24   # candles for daily position

        # Rolling windows to avoid O(n^2) slicing on each iteration
        rolling_lows: List[float] = []
        rolling_highs: List[float] = []
        rolling_vols: List[float] = []

        for i in range(len(candles)):
            price = closes[i]

            # Maintain rolling windows
            rolling_lows.append(lows[i])
            rolling_highs.append(highs[i])
            rolling_vols.append(volumes[i])
            if len(rolling_lows) > lookback:
                rolling_lows.pop(0)
                rolling_highs.pop(0)
            if len(rolling_vols) > 5:
                rolling_vols.pop(0)

            # ── Calculate RSI using shared indicator function ──────────
            rsi_candles = candles[max(0, i + 1 - rsi_period): i + 1]
            rsi = calc_rsi_from_candles(rsi_candles, rsi_period) or 50.0
            rsi_history.append(rsi)
            if len(rsi_history) > 5:
                rsi_history.pop(0)

            rsi_direction = "neutral"
            if len(rsi_history) >= 2:
                if rsi_history[-1] > rsi_history[-2]:
                    rsi_direction = "up"
                elif rsi_history[-1] < rsi_history[-2]:
                    rsi_direction = "down"

            # ── Daily position (from rolling window) ───────────────────
            daily_pos = self._calc_daily_position(price, rolling_lows, rolling_highs)

            # ── Orderbook imbalance (from rolling volume) ─────────────
            imbalance = self._calc_imbalance_from_volume(rolling_vols)

            # ── Price momentum ─────────────────────────────────────────
            price_change_pct = 0.0
            if i >= 1 and closes[i - 1] > 0:
                price_change_pct = ((price - closes[i - 1]) / closes[i - 1]) * 100

            # ── Evaluate LONG/SHORT conditions ─────────────────────────
            sig_type = self._evaluate_v2_signal(
                rsi, rsi_direction, daily_pos, imbalance, price_change_pct
            )

            if sig_type != "NO TRADE SETUP":
                signals.append({
                    "time": timestamps[i],
                    "signal": sig_type,
                    "price": price,
                })

        return signals

    def _evaluate_v2_signal(
        self,
        rsi: float,
        rsi_direction: str,
        daily_pos: float,
        imbalance: float,
        price_change_pct: float,
    ) -> str:
        """Evaluate LONG/SHORT conditions from V2 strategy."""
        # Thresholds (same as strategy_new.py)
        RSI_OVERSOLD_EXIT = 35
        RSI_OVERBOUGHT_EXIT = 65
        DP_BUY_ZONE = 30
        DP_SELL_ZONE = 70

        # ── LONG conditions ───────────────────────────────────────────
        if (
            rsi <= RSI_OVERSOLD_EXIT
            and daily_pos <= DP_BUY_ZONE
            and rsi_direction in ("up", "neutral")
        ):
            return "LONG"

        # ── SHORT conditions ──────────────────────────────────────────
        if (
            rsi >= RSI_OVERBOUGHT_EXIT
            and daily_pos >= DP_SELL_ZONE
            and rsi_direction in ("down", "neutral")
        ):
            return "SHORT"

        return "NO TRADE SETUP"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calc_daily_position(
        self,
        price: float,
        lows: List[float],
        highs: List[float],
        lookback: int = 24,
    ) -> float:
        """Calculate daily position (0-100%) from intraday high/low lookback."""
        if not lows or not highs:
            return 50.0
        recent_lows = lows[-lookback:]
        recent_highs = highs[-lookback:]
        low = min(recent_lows)
        high = max(recent_highs)
        if high <= low:
            return 50.0
        return max(0.0, min(100.0, ((price - low) / (high - low)) * 100))

    def _calc_imbalance_from_volume(self, volumes: List[float], window: int = 5) -> float:
        """Rough orderbook imbalance simulation from volume sequence."""
        if len(volumes) < window:
            return 1.0
        recent = volumes[-window:]
        odd = sum(recent[::2])    # simulate bid volume
        even = sum(recent[1::2])  # simulate ask volume
        if even == 0:
            return 1.0
        return odd / even

    def _days_to_candles(self, days: int, interval: str) -> int:
        """Convert days to approximate candle count for the given interval."""
        seconds_per_candle = {
            "1m": 60, "3m": 180, "5m": 300, "15m": 900,
            "30m": 1800, "1h": 3600, "4h": 14400,
            "1d": 86400, "1w": 604800,
        }
        sec = seconds_per_candle.get(interval, 3600)
        return max(20, (days * 86400) // sec + 10)

    def _close_trade(
        self,
        position: Dict,
        exit_time: float,
        exit_price: float,
        current_equity: float,
    ) -> Trade:
        """Close an open position and compute the trade result."""
        entry_price = position["entry_price"]
        side = position["side"]
        entry_time = position["entry_time"]
        risk_amount = position["risk_amount"]

        # Calculate qty from risk amount
        if side == "LONG":
            stop_distance = entry_price * 0.02   # assume 2% stop
        else:
            stop_distance = entry_price * 0.02

        qty = risk_amount / stop_distance if stop_distance > 0 else 0

        if side == "LONG":
            pnl = (exit_price - entry_price) * qty
        else:
            pnl = (entry_price - exit_price) * qty

        pnl_pct = (pnl / current_equity * 100) if current_equity > 0 else 0

        hold_seconds = exit_time - entry_time
        hold_hours = hold_seconds / 3600

        return Trade(
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            side=side,
            qty=qty,
            pnl=pnl,
            pnl_pct=pnl_pct,
            hold_hours=hold_hours,
        )

    def _compute_stats(
        self,
        trades: List[Trade],
        final_equity: float,
        max_drawdown: float,
    ) -> Dict:
        """Compute performance statistics from completed trades."""
        n = len(trades)

        if n == 0:
            return {
                "pnl": 0.0,
                "pnl_pct": 0.0,
                "win_rate": 0.0,
                "max_drawdown": 0.0,
                "max_drawdown_pct": 0.0,
                "avg_hold_hours": 0.0,
                "best_trade_pct": 0.0,
                "worst_trade_pct": 0.0,
                "sharpe_ratio": 0.0,
                "trade_count": 0,
                "win_count": 0,
                "loss_count": 0,
            }

        pnls = [t.pnl_pct for t in trades]
        wins = [p for p in pnls if p > 0]

        total_pnl = final_equity - self.initial_capital
        pnl_pct = (total_pnl / self.initial_capital) * 100
        win_rate = (len(wins) / n * 100) if n > 0 else 0.0

        best_trade = max(pnls) if pnls else 0.0
        worst_trade = min(pnls) if pnls else 0.0

        avg_hold = sum(t.hold_hours for t in trades) / n if n > 0 else 0.0

        # Sharpe ratio (simplified: avg return / std dev * sqrt(annualized))
        if len(pnls) >= 2:
            mean_ret = sum(pnls) / len(pnls)
            variance = sum((p - mean_ret) ** 2 for p in pnls) / max(1, len(pnls) - 1)
            std_ret = math.sqrt(variance)
            if std_ret > 0:
                sharpe = (mean_ret / std_ret) * math.sqrt(365 * 24)  # hourly data annualized
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        return {
            "pnl": total_pnl,
            "pnl_pct": pnl_pct,
            "win_rate": win_rate,
            "max_drawdown": max_drawdown,
            "max_drawdown_pct": max_drawdown,
            "avg_hold_hours": avg_hold,
            "best_trade_pct": best_trade,
            "worst_trade_pct": worst_trade,
            "sharpe_ratio": sharpe,
            "trade_count": n,
            "win_count": len(wins),
            "loss_count": n - len(wins),
        }

    def _trade_to_dict(self, trade: Trade) -> Dict:
        """Convert Trade dataclass to a plain dict."""
        return {
            "entry_time": trade.entry_time,
            "exit_time": trade.exit_time,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "side": trade.side,
            "qty": trade.qty,
            "pnl": trade.pnl,
            "pnl_pct": trade.pnl_pct,
            "hold_hours": trade.hold_hours,
        }

    def _empty_result(self) -> Dict:
        """Return an empty result when no data is available."""
        return {
            "equity_curve": [],
            "trades": [],
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "win_rate": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_hold_hours": 0.0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
            "sharpe_ratio": 0.0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
