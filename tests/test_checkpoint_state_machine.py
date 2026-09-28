"""
Unit tests for Pure Exit Policy & Nonblocking Checkpoint State Machine.

Validates:
1. Invariant: 100 -> 106 -> 103 trailing stop triggers exit (armed stop survives retracement).
2. Stop prices tighten monotonically, never widen.
3. Directional symmetry for LONG and SHORT exit policies.
4. Nonblocking Checkpoint State Machine transitions:
   STANDARD -> TRAILING_ARMED -> TP_EVALUATING -> RIDING -> CLOSED.
5. Floor breach during TP_EVALUATING triggers immediate protective exit without waiting for AI.
6. Evaluation deadline expiry (3s) triggers deterministic take-profit exit.
7. Late AI responses do not resurrect closed positions or mutate state.
8. Failed sell transitions to EXIT_PENDING and prevents duplicate order submission.
"""

from __future__ import annotations

import concurrent.futures
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from hermes.state import state
from hermes.trading.exit_policy import decide_exit, ExitDecision
from hermes.trading.positions import (
    check_open_positions,
    _active_evaluations,
    EVALUATION_DEADLINE_SECONDS,
)
from tests.support import IsolatedTestCase


class TestCheckpointStateMachine(IsolatedTestCase):
    """Test suite for exit policy and checkpoint state machine."""

    def setUp(self):
        super().setUp()
        state.positions.clear()
        _active_evaluations.clear()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Pure Exit Policy Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_trailing_stop_survives_retracement_100_106_103(self):
        """
        Invariant test:
        Entry 100 -> climbs to 106 (+6%, activates trailing stop) -> retraces to 103 (+3%).
        Even though current profit is below +6% activation, the armed trailing stop
        at 106 * (1 - 0.025) = 103.35 triggers an exit at 103.
        """
        # 1. Price at 100 (entry)
        dec1 = decide_exit(
            entry_price="100.0",
            peak_price="100.0",
            current_price="100.0",
            trailing_armed=False,
            activation_pct="0.06",
            trail_pct="0.025",
            hard_stop_pct="0.05",
            side="LONG",
        )
        self.assertFalse(dec1.should_exit)
        self.assertFalse(dec1.trailing_armed)
        self.assertEqual(dec1.effective_stop_price, Decimal("95.0"))

        # 2. Price climbs to 106 (activation reached!)
        dec2 = decide_exit(
            entry_price="100.0",
            peak_price="106.0",
            current_price="106.0",
            trailing_armed=False,
            activation_pct="0.06",
            trail_pct="0.025",
            hard_stop_pct="0.05",
            side="LONG",
        )
        self.assertFalse(dec2.should_exit)
        self.assertTrue(dec2.trailing_armed)
        # 106 * (1 - 0.025) = 103.35
        self.assertEqual(dec2.effective_stop_price, Decimal("103.35"))

        # 3. Price drops to 103 (below 103.35 stop)
        dec3 = decide_exit(
            entry_price="100.0",
            peak_price="106.0",
            current_price="103.0",
            trailing_armed=dec2.trailing_armed,
            activation_pct="0.06",
            trail_pct="0.025",
            hard_stop_pct="0.05",
            side="LONG",
        )
        self.assertTrue(dec3.should_exit)
        self.assertEqual(dec3.exit_type, "TRAILING_STOP")
        self.assertTrue(dec3.trailing_armed)
        self.assertEqual(dec3.effective_stop_price, Decimal("103.35"))

    def test_monotonic_stop_tightening(self):
        """Stops can only tighten (increase for LONG, decrease for SHORT), never widen."""
        # LONG: higher current_stop cannot be overridden by wider stop
        dec_long = decide_exit(
            entry_price="100.0",
            peak_price="110.0",
            current_price="109.0",
            trailing_armed=True,
            trail_pct="0.05",  # 110 * 0.95 = 104.5
            current_stop="107.0",  # Prior stop was 107.0
            side="LONG",
        )
        # Effective stop must be at least 107.0
        self.assertGreaterEqual(dec_long.effective_stop_price, Decimal("107.0"))

        # SHORT: lower current_stop cannot be overridden by wider stop
        dec_short = decide_exit(
            entry_price="100.0",
            peak_price="90.0",
            current_price="91.0",
            trailing_armed=True,
            trail_pct="0.05",  # 90 * 1.05 = 94.5
            current_stop="92.0",  # Prior stop was 92.0
            side="SHORT",
        )
        # Effective stop must be at most 92.0
        self.assertLessEqual(dec_short.effective_stop_price, Decimal("92.0"))

    def test_short_trailing_stop_symmetry(self):
        """SHORT trailing stop activates when price falls, exits on bounce."""
        # SHORT: entry 100 -> drops to 94 (gain 6%) -> bounces to 97
        dec_short_armed = decide_exit(
            entry_price="100.0",
            peak_price="94.0",
            current_price="94.0",
            trailing_armed=False,
            activation_pct="0.06",
            trail_pct="0.025",
            hard_stop_pct="0.05",
            side="SHORT",
        )
        self.assertTrue(dec_short_armed.trailing_armed)
        # 94 * 1.025 = 96.35
        self.assertEqual(dec_short_armed.effective_stop_price, Decimal("96.35"))

        # Price bounces to 97 (above 96.35)
        dec_short_exit = decide_exit(
            entry_price="100.0",
            peak_price="94.0",
            current_price="97.0",
            trailing_armed=True,
            activation_pct="0.06",
            trail_pct="0.025",
            hard_stop_pct="0.05",
            side="SHORT",
        )
        self.assertTrue(dec_short_exit.should_exit)
        self.assertEqual(dec_short_exit.exit_type, "TRAILING_STOP")

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Checkpoint State Machine Lifecycle & Integration Tests
    # ──────────────────────────────────────────────────────────────────────────

    @patch("hermes.trading.positions.execute_sell")
    @patch("hermes.trading.positions.evaluate_tp_momentum")
    def test_checkpoint_state_machine_happy_path_extend_and_ride(self, mock_eval, mock_sell):
        """
        Tests full state progression:
        STANDARD -> TRAILING_ARMED -> TP_EVALUATING -> RIDING -> CLOSED.
        """
        pair = "SOLUSDT"
        state.positions[pair] = {
            "entry_price": 100.0,
            "qty": 1.0,
            "time": time.time(),
            "peak_price": 100.0,
            "state": "STANDARD",
            "position_lifecycle_id": "test_life_1",
            "position_version": 1,
        }

        # Step 1: Price climbs to 106.0 -> TRAILING_ARMED
        check_open_positions(106.0, {"sol": 1.0}, specific_pair=pair)
        self.assertEqual(state.positions[pair]["state"], "TRAILING_ARMED")
        self.assertTrue(state.positions[pair]["trailing_armed"])
        self.assertEqual(state.positions[pair]["peak_price"], 106.0)

        # Step 2: Price climbs to 110.0 (+10%) -> Transitions to TP_EVALUATING & spawns evaluation
        mock_eval.return_value = {
            "action": "EXTEND_AND_RIDE",
            "confidence": "HIGH",
            "guaranteed_floor_pct": 0.08,
            "profit_floor_trigger_pct": 0.08,
            "recommended_trail_pct": 0.035,
            "reason": "Strong orderbook pressure",
        }
        check_open_positions(110.0, {"sol": 1.0}, specific_pair=pair)
        self.assertEqual(state.positions[pair]["state"], "TP_EVALUATING")
        self.assertTrue(state.positions[pair]["tp_evaluated"])
        self.assertIn(pair, _active_evaluations)

        # Step 3: Next tick processes completed evaluation -> Transitions to RIDING
        # Wait briefly for ThreadPool executor to finish the mock
        _active_evaluations[pair]["future"].result(timeout=1.0)
        check_open_positions(110.5, {"sol": 1.0}, specific_pair=pair)
        self.assertEqual(state.positions[pair]["state"], "RIDING")
        self.assertEqual(state.positions[pair]["mode"], "RIDING_TREND")
        self.assertEqual(state.positions[pair]["profit_floor_pct"], 0.08)

        # Step 4: In RIDING mode, price climbs to 120.0 (+20%) -> Ratchets floor to 15%
        check_open_positions(120.0, {"sol": 1.0}, specific_pair=pair)
        self.assertEqual(state.positions[pair]["profit_floor_pct"], 0.15)

        # Step 5: Price retraces below floor 115.0 to 114.0 -> Dynamic TP Exit
        mock_sell.return_value = (True, "")
        check_open_positions(114.0, {"sol": 1.0}, specific_pair=pair)
        self.assertEqual(state.positions[pair]["state"], "EXIT_PENDING")
        mock_sell.assert_called()

    @patch("hermes.trading.positions.execute_sell")
    @patch("hermes.trading.positions.evaluate_tp_momentum")
    def test_floor_breach_during_tp_evaluating_triggers_immediate_exit(self, mock_eval, mock_sell):
        """
        Test: While position is in TP_EVALUATING, if price suddenly crashes to 105 (below +8% floor),
        the system exits immediately without waiting for AI or accepting any late AI response.
        """
        pair = "ETHUSDT"
        mock_sell.return_value = (True, "")
        # Slow AI mock that would hang
        slow_future = concurrent.futures.Future()

        state.positions[pair] = {
            "entry_price": 100.0,
            "qty": 2.0,
            "time": time.time(),
            "peak_price": 110.0,
            "state": "TP_EVALUATING",
            "trailing_armed": True,
            "tp_evaluated": True,
            "profit_floor_pct": 0.08,
            "evaluation_deadline": time.time() + 3.0,
            "position_lifecycle_id": "life_eth_1",
            "position_version": 1,
        }
        _active_evaluations[pair] = {
            "future": slow_future,
            "started_at": time.time(),
            "deadline": time.time() + 3.0,
            "position_lifecycle_id": "life_eth_1",
            "position_version": 1,
        }

        # Price plunges to 105.0 (below 108.0 floor)
        check_open_positions(105.0, {"eth": 2.0}, specific_pair=pair)

        # Immediate exit triggered
        mock_sell.assert_called_once()
        self.assertEqual(state.positions[pair]["state"], "EXIT_PENDING")
        self.assertNotIn(pair, _active_evaluations)

    @patch("hermes.trading.positions.execute_sell")
    def test_evaluation_deadline_expiry_fails_closed(self, mock_sell):
        """
        Test: If AI or network evaluation takes > 3.0 seconds (deadline expires),
        the state machine fails closed to TAKE PROFIT EXIT immediately.
        """
        pair = "BTCUSDT"
        mock_sell.return_value = (True, "")
        slow_future = concurrent.futures.Future()

        expired_time = time.time() - 4.0
        state.positions[pair] = {
            "entry_price": 50000.0,
            "qty": 0.1,
            "time": time.time(),
            "peak_price": 55000.0,
            "state": "TP_EVALUATING",
            "trailing_armed": True,
            "tp_evaluated": True,
            "evaluation_started_at": expired_time,
            "evaluation_deadline": expired_time + EVALUATION_DEADLINE_SECONDS,
            "position_lifecycle_id": "life_btc_1",
            "position_version": 1,
        }
        _active_evaluations[pair] = {
            "future": slow_future,
            "started_at": expired_time,
            "deadline": expired_time + EVALUATION_DEADLINE_SECONDS,
            "position_lifecycle_id": "life_btc_1",
            "position_version": 1,
        }

        check_open_positions(55100.0, {"btc": 0.1}, specific_pair=pair)

        mock_sell.assert_called_once()
        self.assertEqual(state.positions[pair]["state"], "EXIT_PENDING")
        self.assertNotIn(pair, _active_evaluations)

    @patch("hermes.trading.positions.execute_sell")
    def test_late_ai_response_does_not_resurrect_closed_position(self, mock_sell):
        """
        Test: An evaluation response that finishes after the position was already closed
        is safely discarded and does not resurrect or mutate state.
        """
        pair = "AVAXUSDT"
        # Position was already closed and removed from state.positions
        state.positions.clear()

        # Simulate completed future for already closed position
        completed_future = concurrent.futures.Future()
        completed_future.set_result({"action": "EXTEND_AND_RIDE", "guaranteed_floor_pct": 0.08})

        _active_evaluations[pair] = {
            "future": completed_future,
            "started_at": time.time() - 1.0,
            "deadline": time.time() + 2.0,
            "position_lifecycle_id": "old_life_id",
            "position_version": 1,
        }

        check_open_positions(40.0, {}, specific_pair=pair)

        # Position must NOT be resurrected
        self.assertNotIn(pair, state.positions)
        self.assertNotIn(pair, _active_evaluations)

    @patch("hermes.trading.positions.execute_sell")
    def test_failed_sell_enters_exit_pending_without_duplicate_spam(self, mock_sell):
        """
        Test: When execute_sell fails (e.g. temporary API error), position enters
        EXIT_PENDING and does not trigger duplicate sell orders on rapid ticks.
        """
        pair = "NEARUSDT"
        mock_sell.return_value = (False, "API Rate limit")

        state.positions[pair] = {
            "entry_price": 10.0,
            "qty": 5.0,
            "time": time.time(),
            "peak_price": 10.0,
            "state": "STANDARD",
            "stop_loss": 9.5,
            "position_lifecycle_id": "life_near_1",
            "position_version": 1,
        }

        # Price hits SL at 9.4
        check_open_positions(9.4, {"near": 5.0}, specific_pair=pair)
        self.assertEqual(mock_sell.call_count, 1)
        self.assertEqual(state.positions[pair]["state"], "EXIT_PENDING")
        self.assertEqual(state.positions[pair]["pending_exit_error"], "API Rate limit")

        # Next immediate tick (within 5s cooldown) does NOT spam execute_sell again
        check_open_positions(9.3, {"near": 5.0}, specific_pair=pair)
        self.assertEqual(mock_sell.call_count, 1)


if __name__ == "__main__":
    unittest.main()
