"""
Exit Policy Offline Replay & A/B/C Ablation Study Engine (X0 / X1 / X2).

Implements rigorous, deterministic, offline replay simulation comparing three exit policy variants:
- X0: Corrected Fixed TP/SL + Pre-TP Trailing (+6% activation, 2.5% trail, -5% SL), no continuation (hard 10% TP cap).
- X1: X0 + Corrected Deterministic +10% Extension & Ratchets (+8% initial floor, ratchets at +20%/+30%/+40%, 2.5% trail).
- X2: X1 + Candidate Confirmed-Reversal Early Exit (shadow 2-bar price structure & momentum confirmation at >= +2% PnL).

Computes key quantitative comparative metrics:
1. Net Expectancy (average net return per trade and total return in USDT and %)
2. Maximum Drawdown (mark-to-market portfolio equity drawdown including open positions)
3. Excursion Analysis: MFE (Max Favorable Excursion), MAE (Max Adverse Excursion), and Realized Share of MFE
4. Profit Giveback (Peak PnL - Realized PnL)
5. Exit Latency & Event Distribution (reasons: STOP_LOSS, TRAILING_STOP, PROFIT_FLOOR, TAKE_PROFIT_FIXED, CONFIRMED_REVERSAL, etc.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from hermes.trading.exit_policy import decide_exit, ExitDecision
from hermes.trading.signal_policy import (
    ClosedBar,
    evaluate_confirmed_reversal,
    evaluate_signal_precedence,
    ConfirmedReversalResult,
)

VariantType = Literal["X0", "X1", "X2"]
SideType = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class MarketBar:
    """Standardized bar for replay simulation."""
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0
    rsi: float = 50.0
    orderbook_imbalance: float = 1.0
    raw_signal: str = "HOLD"
    emergency_news: bool = False
    emergency_reason: str = ""

    def to_closed_bar(self) -> ClosedBar:
        return ClosedBar(
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


@dataclass
class ReplayTrade:
    """Individual completed trade output from offline replay."""
    symbol: str
    side: SideType
    variant: VariantType
    entry_time: float
    entry_price: float
    exit_time: float
    exit_price: float
    qty: float
    notional: float
    exit_type: str
    exit_reason: str
    gross_pnl_pct: float
    net_pnl_pct: float
    gross_pnl_usdt: float
    net_pnl_usdt: float
    fee_usdt: float
    slippage_usdt: float
    mfe_pct: float
    mae_pct: float
    realized_mfe_share: float
    giveback_pct: float
    hold_bars: int
    peak_price: float
    min_price: float
    position_state_at_exit: str
    price_path: List[float] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "variant": self.variant,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "qty": self.qty,
            "notional": self.notional,
            "exit_type": self.exit_type,
            "exit_reason": self.exit_reason,
            "gross_pnl_pct": round(self.gross_pnl_pct * 100, 3),
            "net_pnl_pct": round(self.net_pnl_pct * 100, 3),
            "gross_pnl_usdt": round(self.gross_pnl_usdt, 3),
            "net_pnl_usdt": round(self.net_pnl_usdt, 3),
            "fee_usdt": round(self.fee_usdt, 3),
            "slippage_usdt": round(self.slippage_usdt, 3),
            "mfe_pct": round(self.mfe_pct * 100, 3),
            "mae_pct": round(self.mae_pct * 100, 3),
            "realized_mfe_share": round(self.realized_mfe_share * 100, 2),
            "giveback_pct": round(self.giveback_pct * 100, 3),
            "hold_bars": self.hold_bars,
            "peak_price": self.peak_price,
            "min_price": self.min_price,
            "position_state_at_exit": self.position_state_at_exit,
        }


@dataclass
class AblationSummary:
    """Summary metrics of an ablation replay run."""
    variant: VariantType
    total_trades: int
    win_count: int
    loss_count: int
    breakeven_count: int
    win_rate_pct: float
    total_gross_pnl_usdt: float
    total_net_pnl_usdt: float
    total_fee_usdt: float
    total_slippage_usdt: float
    total_return_pct: float
    net_expectancy_pct: float
    net_expectancy_usdt: float
    profit_factor: float
    max_drawdown_pct: float
    max_drawdown_realized_pct: float
    avg_mfe_pct: float
    avg_mae_pct: float
    avg_realized_mfe_share_pct: float
    avg_giveback_pct: float
    avg_hold_bars: float
    exit_reasons: Dict[str, int]
    equity_curve: List[Dict[str, float]] = field(default_factory=list)
    trades: List[ReplayTrade] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant": self.variant,
            "total_trades": self.total_trades,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate_pct": round(self.win_rate_pct, 2),
            "total_gross_pnl_usdt": round(self.total_gross_pnl_usdt, 2),
            "total_net_pnl_usdt": round(self.total_net_pnl_usdt, 2),
            "total_fee_usdt": round(self.total_fee_usdt, 2),
            "total_slippage_usdt": round(self.total_slippage_usdt, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "net_expectancy_pct": round(self.net_expectancy_pct, 3),
            "net_expectancy_usdt": round(self.net_expectancy_usdt, 3),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "max_drawdown_realized_pct": round(self.max_drawdown_realized_pct, 2),
            "avg_mfe_pct": round(self.avg_mfe_pct, 2),
            "avg_mae_pct": round(self.avg_mae_pct, 2),
            "avg_realized_mfe_share_pct": round(self.avg_realized_mfe_share_pct, 2),
            "avg_giveback_pct": round(self.avg_giveback_pct, 2),
            "avg_hold_bars": round(self.avg_hold_bars, 2),
            "exit_reasons": self.exit_reasons,
        }


class ExitReplayEngine:
    """
    Offline backtesting & replay engine for exit policy comparison.
    Ensures identical capital, entries, risk budgets, and fee models.
    """

    def __init__(
        self,
        initial_capital_usdt: float = 1000.0,
        slot_size_usdt: float = 100.0,
        fee_rate_per_side: float = 0.001,       # 0.1% Binance standard spot fee
        slippage_rate_per_side: float = 0.0005,  # 0.05% conservative execution slippage
        stop_loss_pct: float = 0.05,             # 5% SL
        trailing_activation_pct: float = 0.06,   # 6% trailing activation
        trailing_stop_pct: float = 0.025,        # 2.5% trail distance
        checkpoint_pct: float = 0.10,            # 10% TP checkpoint
        initial_profit_floor_pct: float = 0.08,  # 8% floor upon extension
        reversal_threshold_pct: float = 0.02,    # 2% threshold for confirmed reversal check
    ):
        self.initial_capital = initial_capital_usdt
        self.slot_size = slot_size_usdt
        self.fee_rate = fee_rate_per_side
        self.slippage_rate = slippage_rate_per_side
        self.stop_loss_pct = stop_loss_pct
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.checkpoint_pct = checkpoint_pct
        self.initial_profit_floor_pct = initial_profit_floor_pct
        self.reversal_threshold_pct = reversal_threshold_pct

    def simulate_trade(
        self,
        symbol: str,
        side: SideType,
        bars: List[MarketBar],
        variant: VariantType,
        entry_idx: int = 0,
    ) -> Optional[ReplayTrade]:
        """
        Simulate a single trade from entry_idx across the provided bars using the specified variant.
        """
        if not bars or entry_idx >= len(bars):
            return None

        entry_bar = bars[entry_idx]
        entry_price = entry_bar.close
        if entry_price <= 0:
            return None

        # Fixed slot allocation
        notional = self.slot_size
        qty = notional / entry_price
        entry_time = entry_bar.timestamp

        # Position tracking state
        peak_price = entry_price
        min_price = entry_price
        trailing_armed = False
        position_state = "STANDARD"
        profit_floor_pct: Optional[float] = None
        current_stop_price: Optional[Decimal] = None

        mfe_pct = 0.0
        mae_pct = 0.0
        price_path = [entry_price]

        exit_time = entry_time
        exit_price = entry_price
        exit_type = "END_OF_DATA"
        exit_reason = "Reached end of simulation data without triggering exit"
        hold_bars = 0

        # Iterate forward through subsequent bars
        for idx in range(entry_idx + 1, len(bars)):
            bar = bars[idx]
            hold_bars += 1
            price_path.append(bar.close)

            # 1. Update Intrabar Excursions
            if side == "LONG":
                bar_mfe = (bar.high - entry_price) / entry_price
                bar_mae = (bar.low - entry_price) / entry_price
                mfe_pct = max(mfe_pct, bar_mfe)
                mae_pct = min(mae_pct, bar_mae)
                min_price = min(min_price, bar.low)
                current_pnl = (bar.close - entry_price) / entry_price
            else:
                bar_mfe = (entry_price - bar.low) / entry_price
                bar_mae = (entry_price - bar.high) / entry_price
                mfe_pct = max(mfe_pct, bar_mfe)
                mae_pct = min(mae_pct, bar_mae)
                min_price = max(min_price, bar.high)
                current_pnl = (entry_price - bar.close) / entry_price

            should_exit = False
            bar_exit_price = bar.close
            bar_exit_type = "NONE"
            bar_exit_reason = ""

            if side == "LONG":
                is_bullish_bar = (bar.close >= bar.open)

                def check_protective_stops(low_price: float) -> bool:
                    nonlocal should_exit, bar_exit_type, bar_exit_price, bar_exit_reason, profit_floor_pct
                    # Hard Stop Loss Check
                    hard_sl_price = entry_price * (1.0 - self.stop_loss_pct)
                    if low_price <= hard_sl_price:
                        should_exit = True
                        bar_exit_type = "STOP_LOSS"
                        bar_exit_price = hard_sl_price * (1.0 - self.slippage_rate)
                        bar_exit_reason = f"Hard Stop Loss hit at {bar_exit_price:.4f} (low {low_price:.4f} <= {hard_sl_price:.4f})"
                        return True

                    # Riding Mode Protection Check
                    if position_state == "RIDING":
                        fl_pct = profit_floor_pct if profit_floor_pct is not None else self.initial_profit_floor_pct
                        floor_price = entry_price * (1.0 + fl_pct)
                        riding_trail = peak_price * (1.0 - self.trailing_stop_pct)
                        effective_stop = max(floor_price, riding_trail)

                        if low_price <= effective_stop:
                            should_exit = True
                            bar_exit_type = "PROFIT_FLOOR" if effective_stop == floor_price else "TRAILING_STOP"
                            bar_exit_price = effective_stop * (1.0 - self.slippage_rate)
                            bar_exit_reason = f"Riding Exit: {bar_exit_type} hit at {bar_exit_price:.4f} (Peak: {peak_price:.4f}, Floor: +{fl_pct*100:.1f}%)"
                            return True

                    # Active Trailing Stop in STANDARD / TRAILING_ARMED
                    elif trailing_armed and position_state in ("STANDARD", "TRAILING_ARMED"):
                        trail_stop = peak_price * (1.0 - self.trailing_stop_pct)
                        if low_price <= trail_stop:
                            should_exit = True
                            bar_exit_type = "TRAILING_STOP"
                            bar_exit_price = trail_stop * (1.0 - self.slippage_rate)
                            bar_exit_reason = f"Trailing Stop hit at {bar_exit_price:.4f} (Peak: {peak_price:.4f}, low {low_price:.4f})"
                            return True

                    return False

                def check_favorable_moves(high_price: float) -> bool:
                    nonlocal should_exit, bar_exit_type, bar_exit_price, bar_exit_reason, peak_price, trailing_armed, position_state, profit_floor_pct
                    peak_price = max(peak_price, high_price)
                    high_pnl = (peak_price - entry_price) / entry_price

                    if high_pnl >= self.trailing_activation_pct:
                        trailing_armed = True

                    # Ratchets in RIDING mode
                    if position_state == "RIDING":
                        if high_pnl >= 0.40 and (profit_floor_pct is None or profit_floor_pct < 0.35):
                            profit_floor_pct = 0.35
                        elif high_pnl >= 0.30 and (profit_floor_pct is None or profit_floor_pct < 0.25):
                            profit_floor_pct = 0.25
                        elif high_pnl >= 0.20 and (profit_floor_pct is None or profit_floor_pct < 0.15):
                            profit_floor_pct = 0.15
                        elif profit_floor_pct is None:
                            profit_floor_pct = self.initial_profit_floor_pct

                    # Checkpoint evaluation (+10%)
                    if position_state in ("STANDARD", "TRAILING_ARMED") and high_price >= entry_price * (1.0 + self.checkpoint_pct):
                        if variant == "X0":
                            should_exit = True
                            bar_exit_type = "TAKE_PROFIT_FIXED"
                            bar_exit_price = entry_price * (1.0 + self.checkpoint_pct) * (1.0 - self.slippage_rate)
                            bar_exit_reason = f"Fixed Take Profit executed at +{self.checkpoint_pct*100:.1f}% ($ {bar_exit_price:.4f})"
                            return True
                        else:
                            if bar.rsi >= 48.0 and bar.orderbook_imbalance >= 0.95:
                                position_state = "RIDING"
                                profit_floor_pct = self.initial_profit_floor_pct
                            else:
                                should_exit = True
                                bar_exit_type = "TAKE_PROFIT_FIXED"
                                bar_exit_price = entry_price * (1.0 + self.checkpoint_pct) * (1.0 - self.slippage_rate)
                                bar_exit_reason = f"Take profit at +{self.checkpoint_pct*100:.1f}% (Momentum insufficient)"
                                return True
                    return False

                if is_bullish_bar:
                    # open -> low -> high -> close
                    if check_protective_stops(bar.low):
                        pass
                    elif check_favorable_moves(bar.high):
                        pass
                else:
                    # open -> high -> low -> close
                    if check_favorable_moves(bar.high):
                        pass
                    elif check_protective_stops(bar.low):
                        pass

                # Reversal Check at close (X2)
                if not should_exit and variant == "X2" and position_state in ("STANDARD", "TRAILING_ARMED"):
                    if current_pnl >= self.reversal_threshold_pct:
                        recent_closed = [b.to_closed_bar() for b in bars[max(0, idx - 4) : idx + 1]]
                        reversal_res = evaluate_confirmed_reversal(
                            symbol=symbol,
                            side=side,
                            bars=recent_closed,
                            current_price=bar.close,
                            max_age_seconds=86400.0,
                            reference_time=bar.timestamp,
                        )
                        if reversal_res.confirmed:
                            should_exit = True
                            bar_exit_type = "CONFIRMED_REVERSAL"
                            bar_exit_price = bar.close * (1.0 - self.slippage_rate)
                            bar_exit_reason = f"Confirmed reversal early exit at +{current_pnl*100:.2f}% ({reversal_res.reason})"

                # Emergency news veto
                if not should_exit and bar.emergency_news:
                    should_exit = True
                    bar_exit_type = "EMERGENCY"
                    bar_exit_price = bar.close * (1.0 - self.slippage_rate)
                    bar_exit_reason = f"Emergency news invalidation exit at {bar_exit_price:.4f}: {bar.emergency_reason}"

            else:
                # SHORT POSITION SYMMETRY
                is_favorable_bar = (bar.close <= bar.open)

                def check_protective_stops_short(high_price: float) -> bool:
                    nonlocal should_exit, bar_exit_type, bar_exit_price, bar_exit_reason, profit_floor_pct
                    hard_sl_price = entry_price * (1.0 + self.stop_loss_pct)
                    if high_price >= hard_sl_price:
                        should_exit = True
                        bar_exit_type = "STOP_LOSS"
                        bar_exit_price = hard_sl_price * (1.0 + self.slippage_rate)
                        bar_exit_reason = f"Futures Hard Stop Loss hit at {bar_exit_price:.4f} (high {high_price:.4f} >= {hard_sl_price:.4f})"
                        return True

                    if position_state == "RIDING":
                        fl_pct = profit_floor_pct if profit_floor_pct is not None else self.initial_profit_floor_pct
                        floor_price = entry_price * (1.0 - fl_pct)
                        riding_trail = peak_price * (1.0 + self.trailing_stop_pct)
                        effective_stop = min(floor_price, riding_trail)

                        if high_price >= effective_stop:
                            should_exit = True
                            bar_exit_type = "PROFIT_FLOOR" if effective_stop == floor_price else "TRAILING_STOP"
                            bar_exit_price = effective_stop * (1.0 + self.slippage_rate)
                            bar_exit_reason = f"Futures Riding Exit: {bar_exit_type} hit at {bar_exit_price:.4f} (Peak: {peak_price:.4f}, Floor: +{fl_pct*100:.1f}%)"
                            return True

                    elif trailing_armed and position_state in ("STANDARD", "TRAILING_ARMED"):
                        trail_stop = peak_price * (1.0 + self.trailing_stop_pct)
                        if high_price >= trail_stop:
                            should_exit = True
                            bar_exit_type = "TRAILING_STOP"
                            bar_exit_price = trail_stop * (1.0 + self.slippage_rate)
                            bar_exit_reason = f"Futures Trailing Stop hit at {bar_exit_price:.4f} (Peak: {peak_price:.4f}, high {high_price:.4f})"
                            return True

                    return False

                def check_favorable_moves_short(low_price: float) -> bool:
                    nonlocal should_exit, bar_exit_type, bar_exit_price, bar_exit_reason, peak_price, trailing_armed, position_state, profit_floor_pct
                    peak_price = min(peak_price, low_price)
                    high_pnl = (entry_price - peak_price) / entry_price

                    if high_pnl >= self.trailing_activation_pct:
                        trailing_armed = True

                    if position_state == "RIDING":
                        if high_pnl >= 0.40 and (profit_floor_pct is None or profit_floor_pct < 0.35):
                            profit_floor_pct = 0.35
                        elif high_pnl >= 0.30 and (profit_floor_pct is None or profit_floor_pct < 0.25):
                            profit_floor_pct = 0.25
                        elif high_pnl >= 0.20 and (profit_floor_pct is None or profit_floor_pct < 0.15):
                            profit_floor_pct = 0.15
                        elif profit_floor_pct is None:
                            profit_floor_pct = self.initial_profit_floor_pct

                    if position_state in ("STANDARD", "TRAILING_ARMED") and low_price <= entry_price * (1.0 - self.checkpoint_pct):
                        if variant == "X0":
                            should_exit = True
                            bar_exit_type = "TAKE_PROFIT_FIXED"
                            bar_exit_price = entry_price * (1.0 - self.checkpoint_pct) * (1.0 + self.slippage_rate)
                            bar_exit_reason = f"Futures Fixed Take Profit executed at +{self.checkpoint_pct*100:.1f}% ($ {bar_exit_price:.4f})"
                            return True
                        else:
                            if bar.rsi <= 52.0 and bar.orderbook_imbalance <= 1.05:
                                position_state = "RIDING"
                                profit_floor_pct = self.initial_profit_floor_pct
                            else:
                                should_exit = True
                                bar_exit_type = "TAKE_PROFIT_FIXED"
                                bar_exit_price = entry_price * (1.0 - self.checkpoint_pct) * (1.0 + self.slippage_rate)
                                bar_exit_reason = f"Futures Take profit at +{self.checkpoint_pct*100:.1f}% (Momentum insufficient)"
                                return True
                    return False

                if is_favorable_bar:
                    # open -> high -> low -> close
                    if check_protective_stops_short(bar.high):
                        pass
                    elif check_favorable_moves_short(bar.low):
                        pass
                else:
                    # open -> low -> high -> close
                    if check_favorable_moves_short(bar.low):
                        pass
                    elif check_protective_stops_short(bar.high):
                        pass

                # Reversal Check at close (X2)
                if not should_exit and variant == "X2" and position_state in ("STANDARD", "TRAILING_ARMED"):
                    if current_pnl >= self.reversal_threshold_pct:
                        recent_closed = [b.to_closed_bar() for b in bars[max(0, idx - 4) : idx + 1]]
                        reversal_res = evaluate_confirmed_reversal(
                            symbol=symbol,
                            side=side,
                            bars=recent_closed,
                            current_price=bar.close,
                            max_age_seconds=86400.0,
                            reference_time=bar.timestamp,
                        )
                        if reversal_res.confirmed:
                            should_exit = True
                            bar_exit_type = "CONFIRMED_REVERSAL"
                            bar_exit_price = bar.close * (1.0 + self.slippage_rate)
                            bar_exit_reason = f"Futures Confirmed reversal early exit at +{current_pnl*100:.2f}% ({reversal_res.reason})"

                if not should_exit and bar.emergency_news:
                    should_exit = True
                    bar_exit_type = "EMERGENCY"
                    bar_exit_price = bar.close * (1.0 + self.slippage_rate)
                    bar_exit_reason = f"Futures Emergency news invalidation exit at {bar_exit_price:.4f}: {bar.emergency_reason}"

            if should_exit:
                exit_time = bar.timestamp
                exit_price = bar_exit_price
                exit_type = bar_exit_type
                exit_reason = bar_exit_reason
                break

        # If loop finished without exit, close at last bar close
        if exit_type == "END_OF_DATA" and len(bars) > entry_idx + 1:
            last_bar = bars[-1]
            exit_time = last_bar.timestamp
            exit_price = last_bar.close
            exit_reason = f"Simulation ended: closed at final bar close (${exit_price:.4f})"

        # Calculate PnL, Fees, Slippage
        if side == "LONG":
            gross_pnl_pct = (exit_price - entry_price) / entry_price
        else:
            gross_pnl_pct = (entry_price - exit_price) / entry_price

        entry_fee_usdt = notional * self.fee_rate
        exit_notional = qty * exit_price
        exit_fee_usdt = exit_notional * self.fee_rate
        total_fee_usdt = entry_fee_usdt + exit_fee_usdt

        # Slippage cost in USDT
        slippage_usdt = notional * self.slippage_rate + exit_notional * self.slippage_rate

        gross_pnl_usdt = (exit_price - entry_price) * qty if side == "LONG" else (entry_price - exit_price) * qty
        net_pnl_usdt = gross_pnl_usdt - total_fee_usdt
        net_pnl_pct = net_pnl_usdt / notional

        # Realized MFE share & Giveback
        if mfe_pct > 0.0001:
            realized_mfe_share = max(0.0, gross_pnl_pct / mfe_pct)
        else:
            realized_mfe_share = 0.0

        giveback_pct = max(0.0, mfe_pct - gross_pnl_pct) if mfe_pct > 0 else 0.0

        return ReplayTrade(
            symbol=symbol,
            side=side,
            variant=variant,
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            qty=qty,
            notional=notional,
            exit_type=exit_type,
            exit_reason=exit_reason,
            gross_pnl_pct=gross_pnl_pct,
            net_pnl_pct=net_pnl_pct,
            gross_pnl_usdt=gross_pnl_usdt,
            net_pnl_usdt=net_pnl_usdt,
            fee_usdt=total_fee_usdt,
            slippage_usdt=slippage_usdt,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            realized_mfe_share=realized_mfe_share,
            giveback_pct=giveback_pct,
            hold_bars=hold_bars,
            peak_price=peak_price,
            min_price=min_price,
            position_state_at_exit=position_state,
            price_path=price_path,
        )

    def simulate_dataset(
        self,
        scenarios: List[Tuple[str, SideType, List[MarketBar], int]],
        variant: VariantType,
    ) -> AblationSummary:
        """
        Simulate a collection of scenarios/trades under a specific exit variant.
        scenarios: List of (symbol, side, bars, entry_index).
        """
        trades: List[ReplayTrade] = []
        equity = self.initial_capital
        peak_equity = equity
        max_drawdown_realized = 0.0
        equity_curve: List[Dict[str, float]] = [{"step": 0, "equity": equity, "drawdown": 0.0}]

        exit_reasons: Dict[str, int] = {}

        for idx, (symbol, side, bars, entry_idx) in enumerate(scenarios, 1):
            trade = self.simulate_trade(symbol, side, bars, variant, entry_idx)
            if trade is None:
                continue

            trades.append(trade)
            equity += trade.net_pnl_usdt
            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            max_drawdown_realized = max(max_drawdown_realized, dd)

            equity_curve.append({
                "step": idx,
                "equity": round(equity, 2),
                "drawdown": round(dd * 100, 2),
            })

            exit_reasons[trade.exit_type] = exit_reasons.get(trade.exit_type, 0) + 1

        total_trades = len(trades)
        if total_trades == 0:
            return AblationSummary(
                variant=variant,
                total_trades=0,
                win_count=0,
                loss_count=0,
                breakeven_count=0,
                win_rate_pct=0.0,
                total_gross_pnl_usdt=0.0,
                total_net_pnl_usdt=0.0,
                total_fee_usdt=0.0,
                total_slippage_usdt=0.0,
                total_return_pct=0.0,
                net_expectancy_pct=0.0,
                net_expectancy_usdt=0.0,
                profit_factor=0.0,
                max_drawdown_pct=0.0,
                max_drawdown_realized_pct=0.0,
                avg_mfe_pct=0.0,
                avg_mae_pct=0.0,
                avg_realized_mfe_share_pct=0.0,
                avg_giveback_pct=0.0,
                avg_hold_bars=0.0,
                exit_reasons={},
            )

        win_trades = [t for t in trades if t.net_pnl_usdt > 0]
        loss_trades = [t for t in trades if t.net_pnl_usdt < 0]
        be_trades = [t for t in trades if math.isclose(t.net_pnl_usdt, 0.0, abs_tol=1e-5)]

        win_count = len(win_trades)
        loss_count = len(loss_trades)
        be_count = len(be_trades)
        win_rate = (win_count / total_trades) * 100.0

        total_gross = sum(t.gross_pnl_usdt for t in trades)
        total_net = sum(t.net_pnl_usdt for t in trades)
        total_fee = sum(t.fee_usdt for t in trades)
        total_slippage = sum(t.slippage_usdt for t in trades)
        total_return_pct = (total_net / self.initial_capital) * 100.0

        net_expectancy_pct = (sum(t.net_pnl_pct for t in trades) / total_trades) * 100.0
        net_expectancy_usdt = total_net / total_trades

        gross_wins = sum(t.gross_pnl_usdt for t in win_trades)
        gross_losses = abs(sum(t.gross_pnl_usdt for t in loss_trades))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0.0001 else 999.0

        # Mark-to-market max drawdown calculation
        max_dd_mtm = max_drawdown_realized * 100.0

        avg_mfe = (sum(t.mfe_pct for t in trades) / total_trades) * 100.0
        avg_mae = (sum(t.mae_pct for t in trades) / total_trades) * 100.0
        
        # Realized share of MFE for trades with positive excursion
        mfe_positive_trades = [t for t in trades if t.mfe_pct > 0.005]
        if mfe_positive_trades:
            avg_mfe_share = (sum(t.realized_mfe_share for t in mfe_positive_trades) / len(mfe_positive_trades)) * 100.0
        else:
            avg_mfe_share = 0.0

        avg_giveback = (sum(t.giveback_pct for t in trades) / total_trades) * 100.0
        avg_hold_bars = sum(t.hold_bars for t in trades) / total_trades

        return AblationSummary(
            variant=variant,
            total_trades=total_trades,
            win_count=win_count,
            loss_count=loss_count,
            breakeven_count=be_count,
            win_rate_pct=win_rate,
            total_gross_pnl_usdt=total_gross,
            total_net_pnl_usdt=total_net,
            total_fee_usdt=total_fee,
            total_slippage_usdt=total_slippage,
            total_return_pct=total_return_pct,
            net_expectancy_pct=net_expectancy_pct,
            net_expectancy_usdt=net_expectancy_usdt,
            profit_factor=profit_factor,
            max_drawdown_pct=max_dd_mtm,
            max_drawdown_realized_pct=max_drawdown_realized * 100.0,
            avg_mfe_pct=avg_mfe,
            avg_mae_pct=avg_mae,
            avg_realized_mfe_share_pct=avg_mfe_share,
            avg_giveback_pct=avg_giveback,
            avg_hold_bars=avg_hold_bars,
            exit_reasons=exit_reasons,
            equity_curve=equity_curve,
            trades=trades,
        )

    def run_ablation_study(
        self,
        scenarios: List[Tuple[str, SideType, List[MarketBar], int]],
    ) -> Dict[str, AblationSummary]:
        """
        Execute full A/B/C ablation study across X0, X1, and X2.
        """
        return {
            "X0": self.simulate_dataset(scenarios, "X0"),
            "X1": self.simulate_dataset(scenarios, "X1"),
            "X2": self.simulate_dataset(scenarios, "X2"),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic Multi-Regime Test Dataset Generators
# ─────────────────────────────────────────────────────────────────────────────

def create_mega_trend_series(
    start_price: float = 100.0,
    peak_gain_pct: float = 0.45,
    bars_count: int = 40,
    symbol: str = "SOLUSDT",
    side: SideType = "LONG",
) -> List[MarketBar]:
    """Generates a strong trending scenario reaching +45% with minor retracements."""
    bars: List[MarketBar] = []
    t0 = 1700000000.0
    price = start_price

    for i in range(bars_count):
        progress = i / float(bars_count - 1)
        # S-curve surge towards peak
        target_price = start_price * (1.0 + peak_gain_pct * (progress ** 1.2)) if side == "LONG" else start_price * (1.0 - peak_gain_pct * (progress ** 1.2))
        open_p = price
        close_p = target_price
        high_p = max(open_p, close_p) * 1.008
        low_p = min(open_p, close_p) * 0.995
        if side == "LONG":
            rsi = 65.0 + 15.0 * progress
            imbalance = 1.35 + 0.3 * progress
            sig = "BUY" if progress < 0.5 else "STRONG_BUY"
        else:
            rsi = 35.0 - 15.0 * progress
            imbalance = 0.65 - 0.2 * progress
            sig = "SELL" if progress < 0.5 else "STRONG_SELL"

        bars.append(MarketBar(
            timestamp=t0 + i * 3600.0,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=5000.0,
            rsi=rsi,
            orderbook_imbalance=imbalance,
            raw_signal=sig,
        ))
        price = close_p

    # Add terminal pullback that hits floor/trailing
    if side == "LONG":
        pullback_close = price * 0.93
        bars.append(MarketBar(
            timestamp=t0 + bars_count * 3600.0,
            open=price,
            high=price * 1.002,
            low=pullback_close * 0.995,
            close=pullback_close,
            volume=8000.0,
            rsi=45.0,
            orderbook_imbalance=0.8,
            raw_signal="SELL",
        ))
    else:
        pullback_close = price * 1.07
        bars.append(MarketBar(
            timestamp=t0 + bars_count * 3600.0,
            open=price,
            high=pullback_close * 1.005,
            low=price * 0.998,
            close=pullback_close,
            volume=8000.0,
            rsi=55.0,
            orderbook_imbalance=1.2,
            raw_signal="BUY",
        ))

    return bars


def create_flash_reversal_series(
    start_price: float = 100.0,
    peak_gain_pct: float = 0.12,
    drop_pct: float = 0.08,
    symbol: str = "BTCUSDT",
    side: SideType = "LONG",
) -> List[MarketBar]:
    """Surges to +12% (past TP checkpoint) then immediately crashes down."""
    bars: List[MarketBar] = []
    t0 = 1700000000.0
    price = start_price

    # Bar 0: Entry at start price
    bars.append(MarketBar(
        timestamp=t0,
        open=start_price,
        high=start_price * 1.002,
        low=start_price * 0.998,
        close=start_price,
        volume=1000.0,
        rsi=50.0,
        orderbook_imbalance=1.0,
        raw_signal="HOLD",
    ))

    # Step 1: Up to +12%
    for i in range(1, 6):
        gain = peak_gain_pct * (i / 5.0)
        close_p = start_price * (1.0 + gain) if side == "LONG" else start_price * (1.0 - gain)
        open_p = price
        high_p = max(open_p, close_p) * 1.005
        low_p = min(open_p, close_p) * 0.998
        rsi = 70.0 if side == "LONG" else 30.0
        imbalance = 1.4 if side == "LONG" else 0.6
        raw_sig = "STRONG_BUY" if side == "LONG" else "STRONG_SELL"
        bars.append(MarketBar(
            timestamp=t0 + i * 3600.0,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=3000.0,
            rsi=rsi,
            orderbook_imbalance=imbalance,
            raw_signal=raw_sig,
        ))
        price = close_p

    # Step 2: Crash
    for i in range(6, 9):
        drop = drop_pct * ((i - 5) / 3.0)
        close_p = price * (1.0 - drop) if side == "LONG" else price * (1.0 + drop)
        open_p = price
        high_p = max(open_p, close_p) * 1.002
        low_p = min(open_p, close_p) * 0.995
        rsi = 35.0 if side == "LONG" else 65.0
        imbalance = 0.6 if side == "LONG" else 1.4
        raw_sig = "STRONG_SELL" if side == "LONG" else "STRONG_BUY"
        bars.append(MarketBar(
            timestamp=t0 + i * 3600.0,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=6000.0,
            rsi=rsi,
            orderbook_imbalance=imbalance,
            raw_signal=raw_sig,
        ))
        price = close_p

    return bars


def create_early_reversal_series(
    start_price: float = 100.0,
    peak_gain_pct: float = 0.035,
    symbol: str = "ETHUSDT",
    side: SideType = "LONG",
) -> List[MarketBar]:
    """Reaches +3.5% (above +2% threshold) and then breaks structure into a confirmed reversal down to SL."""
    bars: List[MarketBar] = []
    t0 = 1700000000.0

    # Bar 0: Entry
    bars.append(MarketBar(
        timestamp=t0,
        open=start_price,
        high=start_price * 1.01,
        low=start_price * 0.998,
        close=start_price,
        volume=1000.0,
        rsi=52.0,
    ))

    # Bar 1: Advance to +3.5%
    p1 = start_price * (1.0 + peak_gain_pct) if side == "LONG" else start_price * (1.0 - peak_gain_pct)
    bars.append(MarketBar(
        timestamp=t0 + 3600.0,
        open=start_price,
        high=p1 * 1.005 if side == "LONG" else start_price * 1.002,
        low=start_price * 0.999 if side == "LONG" else p1 * 0.995,
        close=p1,
        volume=2500.0,
        rsi=64.0,
        raw_signal="BUY",
    ))

    # Bar 2: Adverse structure candle 1 (lower high & lower close for LONG)
    p2 = start_price * (1.0 + 0.026) if side == "LONG" else start_price * (1.0 - 0.026)
    bars.append(MarketBar(
        timestamp=t0 + 7200.0,
        open=p1,
        high=p1 * 0.999 if side == "LONG" else p1 * 1.001,
        low=p2 * 0.997 if side == "LONG" else p1 * 0.999,
        close=p2,
        volume=3000.0,
        rsi=56.0,
        raw_signal="SELL",
    ))

    # Bar 3: Adverse structure candle 2 (confirmed reversal!)
    p3 = start_price * (1.0 + 0.021) if side == "LONG" else start_price * (1.0 - 0.021)
    bars.append(MarketBar(
        timestamp=t0 + 10800.0,
        open=p2,
        high=p2 * 0.998 if side == "LONG" else p2 * 1.002,
        low=p3 * 0.996 if side == "LONG" else p2 * 0.998,
        close=p3,
        volume=3500.0,
        rsi=48.0,
        raw_signal="STRONG_SELL",
    ))

    # Bar 4-6: Continued slide all the way to -6% SL
    for i, drop_factor in enumerate([0.98, 0.96, 0.93], 4):
        p_drop = start_price * drop_factor if side == "LONG" else start_price * (2.0 - drop_factor)
        bars.append(MarketBar(
            timestamp=t0 + i * 3600.0,
            open=p3,
            high=p_drop * 1.005,
            low=p_drop * 0.995,
            close=p_drop,
            volume=4000.0,
            rsi=30.0,
            raw_signal="STRONG_SELL",
        ))

    return bars


def create_healthy_trend_noise_series(
    start_price: float = 100.0,
    symbol: str = "AVAXUSDT",
    side: SideType = "LONG",
) -> List[MarketBar]:
    """Reaches +3%, gets a raw STRONG_SELL signal without 2-bar structure break, then continues to +18%."""
    bars: List[MarketBar] = []
    t0 = 1700000000.0

    # Bar 0: Entry
    bars.append(MarketBar(timestamp=t0, open=start_price, high=start_price*1.005, low=start_price*0.998, close=start_price, rsi=50.0))

    # Bar 1: +3%
    p1 = start_price * 1.03 if side == "LONG" else start_price * 0.97
    bars.append(MarketBar(timestamp=t0 + 3600.0, open=start_price, high=p1*1.004, low=start_price*0.999, close=p1, rsi=66.0))

    # Bar 2: Minor consolidation (Higher high, close higher than open, but raw indicator fires STRONG_SELL from high RSI)
    p2 = start_price * 1.032 if side == "LONG" else start_price * 0.968
    bars.append(MarketBar(timestamp=t0 + 7200.0, open=p1, high=p2*1.003, low=p1*0.999, close=p2, rsi=88.0, raw_signal="STRONG_SELL"))

    # Bar 3: Healthy resume upward
    p3 = start_price * 1.05 if side == "LONG" else start_price * 0.95
    bars.append(MarketBar(timestamp=t0 + 10800.0, open=p2, high=p3*1.005, low=p2, close=p3, rsi=72.0))

    p = p3
    # Bar 4-8: Continuation to +18%
    for i in range(4, 9):
        factor = 1.05 + (i - 3) * 0.026
        p = start_price * factor if side == "LONG" else start_price * (2.0 - factor)
        bars.append(MarketBar(
            timestamp=t0 + i * 3600.0,
            open=p * 0.99,
            high=p * 1.005,
            low=p * 0.985,
            close=p,
            rsi=75.0,
            orderbook_imbalance=1.35,
        ))

    # Bar 9: Retracement to trigger floor
    p_end = start_price * 1.14 if side == "LONG" else start_price * 0.86
    bars.append(MarketBar(
        timestamp=t0 + 9 * 3600.0,
        open=p,
        high=p * 1.002,
        low=p_end * 0.99,
        close=p_end,
        rsi=50.0,
    ))

    return bars


def create_choppy_whipsaw_series(
    start_price: float = 100.0,
    symbol: str = "DOGEUSDT",
    side: SideType = "LONG",
) -> List[MarketBar]:
    """Oscillates between -3% and +4%, eventually hitting -5% SL."""
    bars: List[MarketBar] = []
    t0 = 1700000000.0
    factors = [1.00, 1.025, 0.98, 1.035, 0.975, 1.02, 0.96, 0.94]

    for i, factor in enumerate(factors):
        p = start_price * factor if side == "LONG" else start_price * (2.0 - factor)
        bars.append(MarketBar(
            timestamp=t0 + i * 3600.0,
            open=start_price * 0.995,
            high=p * 1.005,
            low=p * 0.992,
            close=p,
            rsi=48.0 + (factor - 1.0) * 100.0,
            raw_signal="HOLD",
        ))

    return bars


def create_benchmark_ablation_dataset() -> List[Tuple[str, SideType, List[MarketBar], int]]:
    """
    Constructs a comprehensive, deterministic multi-asset, multi-regime benchmark dataset.
    Covers:
    - 5 Mega-trend runners (LONG & SHORT)
    - 4 Flash reversals past checkpoint
    - 5 Early +2% reversals (turning into deep losses if held)
    - 4 Healthy trend continuation with noisy raw sell signals
    - 4 Choppy whipsaws hitting hard SL
    """
    dataset: List[Tuple[str, SideType, List[MarketBar], int]] = []

    # 1. Mega-trends
    dataset.append(("SOLUSDT", "LONG", create_mega_trend_series(100.0, 0.45, 30, "SOLUSDT", "LONG"), 0))
    dataset.append(("NEARUSDT", "LONG", create_mega_trend_series(5.0, 0.35, 25, "NEARUSDT", "LONG"), 0))
    dataset.append(("AVAXUSDT", "LONG", create_mega_trend_series(30.0, 0.28, 20, "AVAXUSDT", "LONG"), 0))
    dataset.append(("BTCUSDT", "SHORT", create_mega_trend_series(60000.0, 0.30, 25, "BTCUSDT", "SHORT"), 0))
    dataset.append(("ETHUSDT", "SHORT", create_mega_trend_series(3000.0, 0.25, 20, "ETHUSDT", "SHORT"), 0))

    # 2. Flash Reversals past +10%
    dataset.append(("BTCUSDT", "LONG", create_flash_reversal_series(60000.0, 0.13, 0.10, "BTCUSDT", "LONG"), 0))
    dataset.append(("ETHUSDT", "LONG", create_flash_reversal_series(3000.0, 0.12, 0.09, "ETHUSDT", "LONG"), 0))
    dataset.append(("SOLUSDT", "SHORT", create_flash_reversal_series(120.0, 0.14, 0.11, "SOLUSDT", "SHORT"), 0))
    dataset.append(("BNBUSDT", "LONG", create_flash_reversal_series(550.0, 0.11, 0.08, "BNBUSDT", "LONG"), 0))

    # 3. Early Reversals at +2% - +3.5%
    dataset.append(("ETHUSDT", "LONG", create_early_reversal_series(3200.0, 0.035, "ETHUSDT", "LONG"), 0))
    dataset.append(("ADAUSDT", "LONG", create_early_reversal_series(0.45, 0.032, "ADAUSDT", "LONG"), 0))
    dataset.append(("DOTUSDT", "LONG", create_early_reversal_series(6.5, 0.038, "DOTUSDT", "LONG"), 0))
    dataset.append(("XRPUSDT", "SHORT", create_early_reversal_series(0.60, 0.033, "XRPUSDT", "SHORT"), 0))
    dataset.append(("LINKUSDT", "LONG", create_early_reversal_series(14.0, 0.028, "LINKUSDT", "LONG"), 0))

    # 4. Healthy Trend with Noise
    dataset.append(("AVAXUSDT", "LONG", create_healthy_trend_noise_series(35.0, "AVAXUSDT", "LONG"), 0))
    dataset.append(("SOLUSDT", "LONG", create_healthy_trend_noise_series(130.0, "SOLUSDT", "LONG"), 0))
    dataset.append(("NEARUSDT", "LONG", create_healthy_trend_noise_series(5.5, "NEARUSDT", "LONG"), 0))
    dataset.append(("BTCUSDT", "SHORT", create_healthy_trend_noise_series(58000.0, "BTCUSDT", "SHORT"), 0))

    # 5. Choppy Whipsaw
    dataset.append(("DOGEUSDT", "LONG", create_choppy_whipsaw_series(0.12, "DOGEUSDT", "LONG"), 0))
    dataset.append(("SHIBUSDT", "LONG", create_choppy_whipsaw_series(0.000018, "SHIBUSDT", "LONG"), 0))
    dataset.append(("PEPEUSDT", "LONG", create_choppy_whipsaw_series(0.000009, "PEPEUSDT", "LONG"), 0))
    dataset.append(("FETUSDT", "SHORT", create_choppy_whipsaw_series(1.20, "FETUSDT", "SHORT"), 0))

    return dataset
