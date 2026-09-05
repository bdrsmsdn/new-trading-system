"""Futures Autonomous Position Monitor & Auto-Exit Management."""

import time
from typing import Dict, List
from hermes.logging_setup import log
from hermes.trading.futures import get_futures_account_overview, close_futures_position
from hermes.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_ACTIVATION_PCT, TRAILING_STOP_PCT

# Track highest/lowest PnL reached per position in memory
_futures_peaks: Dict[str, float] = {}

def check_open_futures_positions() -> None:
    """Monitor active Binance Futures positions and trigger autonomous TP/SL/Trailing Stop.
    
    Rules:
    - SL: -2.5% on margin
    - TP: +5.0% on margin
    - Trailing Stop: Starts at +3.0% gain, closes if profit drops 1.5% from peak.
    """
    try:
        overview = get_futures_account_overview()
        if not overview.get("success"):
            return

        positions = overview.get("open_positions", [])
        if not positions:
            _futures_peaks.clear()
            return

        for pos in positions:
            symbol = pos.get("symbol", "")
            pair = pos.get("pair", "")
            side = pos.get("side", "LONG")
            amount = pos.get("amount", 0.0)
            entry_price = pos.get("entry_price", 0.0)
            unrealized_pnl = pos.get("unrealized_pnl", 0.0)
            leverage = pos.get("leverage", 3)

            if entry_price <= 0 or amount <= 0:
                continue

            # Initial Margin used
            notional = amount * entry_price
            initial_margin = notional / leverage if leverage > 0 else notional

            # Return on Equity (ROE %)
            roe_pct = (unrealized_pnl / initial_margin) if initial_margin > 0 else 0.0

            pos_key = f"{symbol}_{side}"
            peak_roe = _futures_peaks.get(pos_key, roe_pct)
            if roe_pct > peak_roe:
                peak_roe = roe_pct
                _futures_peaks[pos_key] = peak_roe

            # 1. Take Profit (+5.0% ROE)
            if roe_pct >= 0.05:
                log.info(f"🎯 [FUTURES-TP] {pos_key}: Hit TP at ROE +{roe_pct*100:.2f}% (PnL: ${unrealized_pnl:+.2f})")
                close_futures_position(pair=pair, side=side, qty=amount, reason=f"Take Profit (+{roe_pct*100:.1f}%)")
                _futures_peaks.pop(pos_key, None)
                continue

            # 2. Trailing Stop (Active when ROE >= +3.0%, triggers on 1.5% pullback)
            if peak_roe >= 0.03:
                if roe_pct <= (peak_roe - 0.015):
                    log.info(f"🛡️ [FUTURES-TRAIL] {pos_key}: Trailing Stop hit! Current ROE +{roe_pct*100:.2f}% (Peak: +{peak_roe*100:.2f}%)")
                    close_futures_position(pair=pair, side=side, qty=amount, reason=f"Trailing Stop (Peak +{peak_roe*100:.1f}%)")
                    _futures_peaks.pop(pos_key, None)
                    continue

            # 3. Stop Loss (-2.5% ROE)
            if roe_pct <= -0.025:
                log.info(f"🛑 [FUTURES-SL] {pos_key}: Stop Loss hit at ROE {roe_pct*100:.2f}% (PnL: ${unrealized_pnl:+.2f})")
                close_futures_position(pair=pair, side=side, qty=amount, reason=f"Stop Loss ({roe_pct*100:.1f}%)")
                _futures_peaks.pop(pos_key, None)
                continue

    except Exception as e:
        log.error(f"[FUTURES-MONITOR-ERROR] {e}")
