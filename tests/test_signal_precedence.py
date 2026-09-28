"""
Unit tests for Signal Precedence and Confirmed Reversal Policy.

Covers:
1. Hard SL hit with STRONG_BUY -> exits immediately (Tier 2), overrides STRONG_BUY.
2. +2% profit with raw STRONG_SELL and healthy trend -> HOLD (ignores raw indicator).
3. +2% profit with confirmed reversal (2 closed bars) -> EXIT_CONFIRMED_REVERSAL.
4. Below +2% with confirmed material invalidation -> EXIT_EMERGENCY (Tier 3).
5. RIDING state floor breach & emergency veto precedence.
6. Rejection of duplicate timestamps and stale candle bars for reversal confirmation.
7. Short position precedence in Futures.
"""

import time
import unittest

from hermes.trading.signal_policy import (
    ClosedBar,
    evaluate_confirmed_reversal,
    evaluate_signal_precedence,
    SignalPrecedenceDecision,
)


class TestSignalPrecedence(unittest.TestCase):
    """Test suite for signal policy precedence tiers."""

    def test_hard_sl_hit_with_strong_buy_exits_immediately(self):
        """Hard Stop Loss must immediately trigger protective exit, overriding STRONG_BUY."""
        decision = evaluate_signal_precedence(
            symbol="BTCUSDT",
            side="LONG",
            entry_price=100.0,
            current_price=94.0,  # -6.0% PnL, breaches 5.0% SL
            hard_stop_pct=0.05,
            raw_signal="STRONG_BUY",
            signal_score=10
        )
        self.assertEqual(decision.action, "EXIT_PROTECTIVE")
        self.assertEqual(decision.tier, 2)
        self.assertTrue(decision.is_protective)
        self.assertEqual(decision.overridden_signal, "STRONG_BUY")
        self.assertIn("Hard Stop Loss hit", decision.reason)

    def test_raw_strong_sell_at_plus_two_pct_with_healthy_trend_holds(self):
        """At +2% profit, raw STRONG_SELL with healthy trend does NOT exit."""
        now = time.time()
        # 2 closed bars showing healthy uptrend (higher highs & higher closes)
        bars = [
            ClosedBar(timestamp=now - 120, open=100.0, high=102.0, low=99.5, close=101.5),
            ClosedBar(timestamp=now - 60, open=101.5, high=103.5, low=101.0, close=103.0),
        ]
        decision = evaluate_signal_precedence(
            symbol="SOLUSDT",
            side="LONG",
            entry_price=100.0,
            current_price=103.0,  # +3.0% PnL
            bars=bars,
            raw_signal="STRONG_SELL",
            signal_score=-5,
            reference_time=now
        )
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.tier, 5)
        self.assertFalse(decision.reversal_confirmed)
        self.assertEqual(decision.overridden_signal, "STRONG_SELL")
        self.assertIn("ignored: trend remains healthy", decision.reason)

    def test_plus_two_pct_with_confirmed_reversal_exits(self):
        """At +2% profit with confirmed adverse structure break across 2 bars, exit is permitted."""
        now = time.time()
        # 2 closed bars showing lower highs and lower closes with downward momentum
        bars = [
            ClosedBar(timestamp=now - 120, open=105.0, high=106.0, low=103.0, close=103.5),
            ClosedBar(timestamp=now - 60, open=103.5, high=104.0, low=101.5, close=102.0),
        ]
        decision = evaluate_signal_precedence(
            symbol="ETHUSDT",
            side="LONG",
            entry_price=100.0,
            current_price=102.0,  # +2.0% PnL
            bars=bars,
            raw_signal="STRONG_SELL",
            signal_score=-5,
            reference_time=now
        )
        self.assertEqual(decision.action, "EXIT_CONFIRMED_REVERSAL")
        self.assertEqual(decision.tier, 5)
        self.assertTrue(decision.reversal_confirmed)
        self.assertIn("Confirmed reversal at +2.00%", decision.reason)

    def test_emergency_invalidation_below_plus_two_pct_exits(self):
        """Emergency news/thesis invalidation triggers EXIT_EMERGENCY even below +2%."""
        decision = evaluate_signal_precedence(
            symbol="AVAXUSDT",
            side="LONG",
            entry_price=100.0,
            current_price=100.5,  # +0.5% PnL
            emergency_veto=True,
            emergency_reason="Severe exploit reported on bridge",
            raw_signal="BUY"
        )
        self.assertEqual(decision.action, "EXIT_EMERGENCY")
        self.assertEqual(decision.tier, 3)
        self.assertTrue(decision.is_protective)
        self.assertIn("Severe exploit reported", decision.reason)

    def test_riding_state_floor_breach_precedes_signals(self):
        """In RIDING state, floor breach triggers EXIT_PROTECTIVE regardless of signals."""
        decision = evaluate_signal_precedence(
            symbol="DOGEUSDT",
            side="LONG",
            entry_price=100.0,
            current_price=107.0,  # +7.0% PnL
            peak_price=120.0,     # Peak was +20%
            profit_floor_pct=0.08, # Floor is +8%
            position_state="RIDING",
            raw_signal="STRONG_BUY"
        )
        self.assertEqual(decision.action, "EXIT_PROTECTIVE")
        self.assertEqual(decision.tier, 2)
        self.assertTrue(decision.is_protective)
        self.assertIn("Protected Profit Floor breached", decision.reason)

    def test_duplicate_bar_timestamps_fails_closed(self):
        """Duplicate bar timestamps must not produce artificial confirmation."""
        now = time.time()
        # Same timestamp on both bars
        bars = [
            ClosedBar(timestamp=now - 60, open=105.0, high=106.0, low=103.0, close=103.0),
            ClosedBar(timestamp=now - 60, open=103.0, high=104.0, low=101.0, close=101.0),
        ]
        reversal = evaluate_confirmed_reversal("BTCUSDT", "LONG", bars, 101.0, reference_time=now)
        self.assertFalse(reversal.confirmed)
        self.assertEqual(reversal.reason, "DUPLICATE_BAR_TIMESTAMPS")

    def test_stale_bar_data_fails_closed(self):
        """Stale bar data (> 300s old) must be rejected."""
        now = time.time()
        bars = [
            ClosedBar(timestamp=now - 600, open=105.0, high=106.0, low=103.0, close=103.0),
            ClosedBar(timestamp=now - 400, open=103.0, high=104.0, low=101.0, close=101.0),
        ]
        reversal = evaluate_confirmed_reversal("BTCUSDT", "LONG", bars, 101.0, max_age_seconds=300.0, reference_time=now)
        self.assertFalse(reversal.confirmed)
        self.assertIn("STALE_BAR_DATA", reversal.reason)

    def test_short_position_hard_sl_precedence(self):
        """Short position reaching hard SL triggers EXIT_PROTECTIVE."""
        decision = evaluate_signal_precedence(
            symbol="BTCUSDT",
            side="SHORT",
            entry_price=100.0,
            current_price=106.0,  # Price rose 6%, SL is 5%
            hard_stop_pct=0.05,
            raw_signal="STRONG_SELL",
            signal_score=-10
        )
        self.assertEqual(decision.action, "EXIT_PROTECTIVE")
        self.assertEqual(decision.tier, 2)
        self.assertIn("Futures Hard Stop Loss hit", decision.reason)


if __name__ == "__main__":
    unittest.main()
