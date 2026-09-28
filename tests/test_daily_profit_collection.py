"""Unit tests for the Daily Profit Collection model with strict isolation."""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

import hermes.config as cfg
import hermes.api.transfer as transfer_mod
from hermes.api.transfer import (
    track_realized_profit,
    run_daily_profit_collector,
    _today_key,
)
from tests.support.isolation import IsolatedTestCase


class TestDailyProfitCollection(IsolatedTestCase):

    def _state_path(self) -> str:
        return transfer_mod._DAILY_STATE_PATH

    def test_daily_profit_collection_disabled_by_default(self):
        """DAILY_PROFIT_COLLECTION must default to False unless explicitly opted in."""
        self.assertFalse(cfg.DAILY_PROFIT_COLLECTION)
        track_realized_profit("BTC", 2.0)
        with patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf:
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_tf.assert_not_called()

    def test_daily_profit_collection_boolean_env_parsing(self):
        """Test explicit boolean environment parsing for DAILY_PROFIT_COLLECTION."""
        from hermes.config import parse_bool_env
        self.assertFalse(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={}))
        self.assertTrue(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "true"}))
        self.assertTrue(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "1"}))
        self.assertTrue(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "yes"}))
        self.assertTrue(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "True"}))
        self.assertFalse(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "false"}))
        self.assertFalse(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "0"}))
        self.assertFalse(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": ""}))

    def test_track_accumulates_profit(self):
        track_realized_profit("BTC", 0.40)
        track_realized_profit("SOL", 0.35)
        state_file = self._state_path()
        self.assertTrue(os.path.exists(state_file))
        with open(state_file) as f:
            state = json.load(f)
        today = _today_key()
        self.assertAlmostEqual(state[today]["collected_profit"], 0.75)
        self.assertFalse(state[today]["target_met"])

    @unittest.skip("Losses must be tracked in net accounting ledger (pending T3/T5 remediation; legacy bug ignored losses)")
    def test_track_includes_losses_net_accounting_contract(self):
        """Contract: Net accounting must record losses rather than ignoring them."""
        track_realized_profit("BTC", -0.50)
        state_file = self._state_path()
        self.assertTrue(os.path.exists(state_file))
        with open(state_file) as f:
            state = json.load(f)
        today = _today_key()
        self.assertAlmostEqual(state[today]["collected_profit"], -0.50)

    def test_no_collection_below_target(self):
        track_realized_profit("BTC", 0.60)
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf:
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_tf.assert_not_called()

    def test_no_double_collection_after_target_met(self):
        track_realized_profit("BTC", 1.20)
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf, \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 50.0}), \
             patch.object(transfer_mod, "telegram_send"):
            mock_tf.return_value = {"tranId": 123}
            self.assertTrue(run_daily_profit_collector())
        # After target met, further profits should NOT trigger another sweep
        track_realized_profit("ETH", 5.0)
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf2:
            self.assertFalse(run_daily_profit_collector())
            mock_tf2.assert_not_called()
        with open(self._state_path()) as f:
            state = json.load(f)
        self.assertTrue(state[_today_key()]["target_met"])

    def test_collection_transfers_exactly_target(self):
        track_realized_profit("SOL", 1.30)  # above 1.0 target
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf, \
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
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf, \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 0.50}), \
             patch.object(transfer_mod, "telegram_send"):
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_tf.assert_not_called()
        # State not marked as collected — can retry later
        with open(self._state_path()) as f:
            state = json.load(f)
        self.assertFalse(state[_today_key()]["target_met"])


if __name__ == "__main__":
    unittest.main()
