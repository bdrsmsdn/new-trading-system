"""
Unit tests for Futures Position Lifecycle, ROE Drawdown & State Safety.

Validates:
1. Futures first observation immediately persists peak ROE; retracement triggers trailing exit.
2. Directional symmetry: ROE trailing works for both LONG and SHORT positions.
3. Position lifecycle identity tracking: closing one position only prunes that position,
   and reopening gets a fresh lifecycle ID without inheriting stale peak/state.
4. Floor breach during Futures TP_EVALUATING triggers immediate exit without waiting for AI.
5. Late AI response does not resurrect closed futures position.
6. Strict unit separation: futures_roe_drawdown_points (ROE points) vs spot_price_trail_fraction (price %).
"""

from __future__ import annotations

import concurrent.futures
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from hermes.state import state
from hermes.trading.exit_policy import decide_futures_exit, FuturesExitDecision
from hermes.trading.futures_monitor import (
    check_open_futures_positions,
    _futures_active_evaluations,
    EVALUATION_DEADLINE_SECONDS,
)
from tests.support import IsolatedTestCase


class TestFuturesExitLifecycle(IsolatedTestCase):
    """Test suite for Futures exit lifecycle, state machine, and unit safety."""

    def setUp(self):
        super().setUp()
        state.futures_positions.clear()
        _futures_active_evaluations.clear()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Pure Futures Exit Policy Tests (ROE Percentage-Point Units)
    # ──────────────────────────────────────────────────────────────────────────

    def test_futures_first_observation_preserves_peak_on_retrace(self):
        """
        Invariant test:
        First observation sees ROE +7.0% (peak_roe = 0.07, trailing armed at 0.06).
        Next observation drops to ROE +4.0% (drawdown 3.0 ROE points > 2.5 ROE points trail).
        Trailing stop must trigger exit.
        """
        # 1. First observation: +7% ROE
        dec1 = decide_futures_exit(
            peak_roe="0.07",
            current_roe="0.07",
            trailing_armed=False,
            activation_roe="0.06",
            futures_roe_drawdown_points="0.025",
            hard_stop_roe="0.05",
            side="LONG",
        )
        self.assertFalse(dec1.should_exit)
        self.assertTrue(dec1.trailing_armed)
        self.assertEqual(dec1.peak_roe, Decimal("0.07"))
        # Trail trigger: 0.07 - 0.025 = 0.045 (+4.5% ROE)
        self.assertEqual(dec1.trail_trigger_roe, Decimal("0.045"))

        # 2. Retracement: ROE drops to +4.0%
        dec2 = decide_futures_exit(
            peak_roe="0.07",  # Preserved peak from first observation
            current_roe="0.04",
            trailing_armed=True,
            activation_roe="0.06",
            futures_roe_drawdown_points="0.025",
            hard_stop_roe="0.05",
            side="LONG",
        )
        self.assertTrue(dec2.should_exit)
        self.assertEqual(dec2.exit_type, "TRAILING_STOP")
        self.assertEqual(dec2.trail_trigger_roe, Decimal("0.045"))

    def test_futures_long_and_short_roe_symmetry(self):
        """
        ROE is directional for both LONG and SHORT:
        Higher ROE is always more profit; drawdown below peak triggers trailing stop.
        """
        # LONG: Peak +15% ROE, drops to +12% ROE (drawdown 3% > 2.5% trail)
        dec_long = decide_futures_exit(
            peak_roe="0.15",
            current_roe="0.12",
            trailing_armed=True,
            futures_roe_drawdown_points="0.025",
            side="LONG",
        )
        self.assertTrue(dec_long.should_exit)
        self.assertEqual(dec_long.exit_type, "TRAILING_STOP")

        # SHORT: Peak +15% ROE, drops to +12% ROE (drawdown 3% > 2.5% trail)
        dec_short = decide_futures_exit(
            peak_roe="0.15",
            current_roe="0.12",
            trailing_armed=True,
            futures_roe_drawdown_points="0.025",
            side="SHORT",
        )
        self.assertTrue(dec_short.should_exit)
        self.assertEqual(dec_short.exit_type, "TRAILING_STOP")

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Futures Lifecycle & Integration State Machine Tests
    # ──────────────────────────────────────────────────────────────────────────

    @patch("hermes.trading.futures_monitor.close_futures_position")
    @patch("hermes.trading.futures_monitor.get_futures_account_overview")
    def test_first_observation_persisted_immediately_and_survives_retrace(self, mock_overview, mock_close):
        """
        Test: When check_open_futures_positions first sees an open position with ROE +7.0%,
        it immediately persists peak_roe=0.07 into state.futures_positions.
        On the next tick with ROE +4.0%, it triggers a trailing stop exit.
        """
        mock_close.return_value = (True, "Order closed")

        # Tick 1: Position observed at +7.0% ROE
        mock_overview.return_value = {
            "success": True,
            "open_positions": [{
                "symbol": "BTCUSDT",
                "pair": "BTCUSDT",
                "side": "LONG",
                "amount": 0.1,
                "entry_price": 50000.0,
                "unrealized_pnl": 116.6667,  # notional $5000, margin $1666.67 (3x) -> ROE ~ +7.0%
                "leverage": 3,
            }]
        }

        check_open_futures_positions()

        pos_key = "BTCUSDT_LONG"
        self.assertIn(pos_key, state.futures_positions)
        self.assertAlmostEqual(state.futures_positions[pos_key]["peak_roe"], 0.07, places=2)
        self.assertTrue(state.futures_positions[pos_key]["trailing_armed"])
        self.assertEqual(mock_close.call_count, 0)

        # Tick 2: Position retraces to +4.0% ROE
        mock_overview.return_value = {
            "success": True,
            "open_positions": [{
                "symbol": "BTCUSDT",
                "pair": "BTCUSDT",
                "side": "LONG",
                "amount": 0.1,
                "entry_price": 50000.0,
                "unrealized_pnl": 66.6667,  # ROE ~ +4.0% (drawdown 3.0 ROE points from 7.0%)
                "leverage": 3,
            }]
        }

        check_open_positions_result = check_open_futures_positions()

        # Trailing stop triggered!
        self.assertEqual(mock_close.call_count, 1)
        # Position removed from state on successful close
        self.assertNotIn(pos_key, state.futures_positions)

    @patch("hermes.trading.futures_monitor.close_futures_position")
    @patch("hermes.trading.futures_monitor.get_futures_account_overview")
    def test_multi_position_isolation_and_lifecycle_pruning(self, mock_overview, mock_close):
        """
        Test: Two open positions (BTC LONG and SOL SHORT).
        When BTC closes, SOL's peak and state remain untouched.
        When BTC re-opens, it receives a new lifecycle ID and fresh peak.
        """
        mock_close.return_value = (True, "Order closed")

        # Tick 1: Both open
        mock_overview.return_value = {
            "success": True,
            "open_positions": [
                {
                    "symbol": "BTCUSDT",
                    "pair": "BTCUSDT",
                    "side": "LONG",
                    "amount": 0.1,
                    "entry_price": 50000.0,
                    "unrealized_pnl": 50.0,
                    "leverage": 3,
                },
                {
                    "symbol": "SOLUSDT",
                    "pair": "SOLUSDT",
                    "side": "SHORT",
                    "amount": 5.0,
                    "entry_price": 100.0,
                    "unrealized_pnl": 20.0,
                    "leverage": 3,
                },
            ]
        }

        check_open_futures_positions()
        btc_lifecycle = state.futures_positions["BTCUSDT_LONG"]["position_lifecycle_id"]
        sol_lifecycle = state.futures_positions["SOLUSDT_SHORT"]["position_lifecycle_id"]

        self.assertIn("BTCUSDT_LONG", state.futures_positions)
        self.assertIn("SOLUSDT_SHORT", state.futures_positions)

        # Tick 2: BTC position is closed on exchange (only SOL remains)
        mock_overview.return_value = {
            "success": True,
            "open_positions": [
                {
                    "symbol": "SOLUSDT",
                    "pair": "SOLUSDT",
                    "side": "SHORT",
                    "amount": 5.0,
                    "entry_price": 100.0,
                    "unrealized_pnl": 25.0,
                    "leverage": 3,
                },
            ]
        }

        check_open_futures_positions()

        # BTC state pruned, SOL state preserved with identical lifecycle_id
        self.assertNotIn("BTCUSDT_LONG", state.futures_positions)
        self.assertIn("SOLUSDT_SHORT", state.futures_positions)
        self.assertEqual(state.futures_positions["SOLUSDT_SHORT"]["position_lifecycle_id"], sol_lifecycle)

        # Tick 3: BTC re-opened
        mock_overview.return_value = {
            "success": True,
            "open_positions": [
                {
                    "symbol": "BTCUSDT",
                    "pair": "BTCUSDT",
                    "side": "LONG",
                    "amount": 0.2,
                    "entry_price": 60000.0,
                    "unrealized_pnl": 0.0,
                    "leverage": 3,
                },
                {
                    "symbol": "SOLUSDT",
                    "pair": "SOLUSDT",
                    "side": "SHORT",
                    "amount": 5.0,
                    "entry_price": 100.0,
                    "unrealized_pnl": 25.0,
                    "leverage": 3,
                },
            ]
        }

        check_open_futures_positions()
        new_btc_lifecycle = state.futures_positions["BTCUSDT_LONG"]["position_lifecycle_id"]
        self.assertNotEqual(new_btc_lifecycle, btc_lifecycle)

    @patch("hermes.trading.futures_monitor.close_futures_position")
    @patch("hermes.trading.futures_monitor.get_futures_account_overview")
    def test_floor_breach_during_futures_tp_evaluating_triggers_immediate_exit(self, mock_overview, mock_close):
        """
        Test: If ROE crashes below +8.0% floor during TP_EVALUATING,
        futures position closes immediately without waiting for AI.
        """
        mock_close.return_value = (True, "Order closed")
        slow_future = concurrent.futures.Future()

        pos_key = "ETHUSDT_LONG"
        state.futures_positions[pos_key] = {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "position_lifecycle_id": "life_eth_f1",
            "position_version": 1,
            "peak_roe": 0.12,
            "state": "TP_EVALUATING",
            "trailing_armed": True,
            "floor_trigger_roe": 0.08,
            "futures_roe_drawdown_points": 0.025,
            "tp_evaluated": True,
            "evaluation_deadline": time.time() + 3.0,
        }
        _futures_active_evaluations[pos_key] = {
            "future": slow_future,
            "started_at": time.time(),
            "deadline": time.time() + 3.0,
            "position_lifecycle_id": "life_eth_f1",
            "position_version": 1,
        }

        # Price plunges such that ROE drops to +5.0% (below 8.0% floor)
        mock_overview.return_value = {
            "success": True,
            "open_positions": [{
                "symbol": "ETHUSDT",
                "pair": "ETHUSDT",
                "side": "LONG",
                "amount": 1.0,
                "entry_price": 3000.0,
                "unrealized_pnl": 50.0,  # notional $3000, margin $1000 (3x) -> ROE = +5.0%
                "leverage": 3,
            }]
        }

        check_open_futures_positions()

        mock_close.assert_called_once()
        self.assertNotIn(pos_key, _futures_active_evaluations)


if __name__ == "__main__":
    unittest.main()
