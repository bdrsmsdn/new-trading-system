"""Unit tests for the Daily Profit Collection model."""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hermes.api.transfer as transfer_mod
from hermes.api.transfer import (
    track_realized_profit,
    run_daily_profit_collector,
    _today_key,
    _DAILY_STATE_PATH,
)


class TestDailyProfitCollection(unittest.TestCase):

    def setUp(self):
        # Fresh state file per test
        if os.path.exists(_DAILY_STATE_PATH):
            os.remove(_DAILY_STATE_PATH)

    def tearDown(self):
        if os.path.exists(_DAILY_STATE_PATH):
            os.remove(_DAILY_STATE_PATH)

    def test_track_accumulates_profit(self):
        track_realized_profit("BTC", 0.40)
        track_realized_profit("SOL", 0.35)
        with open(_DAILY_STATE_PATH) as f:
            state = json.load(f)
        today = _today_key()
        self.assertAlmostEqual(state[today]["collected_profit"], 0.75)
        self.assertFalse(state[today]["target_met"])

    def test_track_ignores_losses(self):
        track_realized_profit("BTC", -0.50)
        # No state file should have been created for a losing trade
        self.assertFalse(os.path.exists(_DAILY_STATE_PATH))

    def test_no_collection_below_target(self):
        track_realized_profit("BTC", 0.60)
        with patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf:
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_tf.assert_not_called()

    def test_no_double_collection_after_target_met(self):
        track_realized_profit("BTC", 1.20)
        with patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf, \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 50.0}), \
             patch.object(transfer_mod, "telegram_send"):
            mock_tf.return_value = {"tranId": 123}
            self.assertTrue(run_daily_profit_collector())
        # After target met, further profits should NOT trigger another sweep
        track_realized_profit("ETH", 5.0)
        with patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf2:
            self.assertFalse(run_daily_profit_collector())
            mock_tf2.assert_not_called()
        with open(_DAILY_STATE_PATH) as f:
            state = json.load(f)
        self.assertTrue(state[_today_key()]["target_met"])

    def test_collection_transfers_exactly_target(self):
        track_realized_profit("SOL", 1.30)  # above 1.0 target
        with patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf, \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 50.0}), \
             patch.object(transfer_mod, "telegram_send"):
            mock_tf.return_value = {"tranId": 456}
            result = run_daily_profit_collector()
        self.assertTrue(result)
        # Exactly the target (1.0), not the full accumulated amount
        args, kwargs = mock_tf.call_args
        self.assertEqual(kwargs.get("amount"), 1.0)

    def test_collection_deferred_when_spot_low(self):
        track_realized_profit("SOL", 1.20)
        with patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf, \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 0.50}), \
             patch.object(transfer_mod, "telegram_send"):
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_tf.assert_not_called()
        # State not marked as collected — can retry later
        with open(_DAILY_STATE_PATH) as f:
            state = json.load(f)
        self.assertFalse(state[_today_key()]["target_met"])


if __name__ == "__main__":
    unittest.main()
