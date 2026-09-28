"""
Unit tests for Conviction Sizing, Dust Filtering, and Active Capital Rotation with test isolation.
"""

import unittest
from unittest.mock import patch, MagicMock

import hermes.config as cfg
from hermes.indicators.volatility import get_dynamic_position_size
import hermes.trading.rotation as rotation_mod
from hermes.trading.rotation import (
    can_rotate_now,
    evaluate_sluggish_positions,
    get_position_momentum_score,
)
from hermes.state import state
from tests.support.isolation import IsolatedTestCase


class TestSizingAndRotation(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        # Set test state positions inside isolated test environment
        state.positions = {
            "BTC": {
                "entry_price": 85000.0,
                "qty": 0.001,
                "time": 1000.0,
                "mode": "STANDARD",
                "stop_loss": 80750.0,
                "take_profit": 93500.0
            },
            "SOL": {
                "entry_price": 120.0,
                "qty": 0.5,
                "time": 1000.0,
                "mode": "STANDARD",
                "stop_loss": 114.0,
                "take_profit": 132.0
            }
        }

    def test_rotation_disabled_by_default(self):
        """ROTATION_ENABLED must default to False unless explicitly opted in."""
        self.assertFalse(cfg.ROTATION_ENABLED)
        self.assertFalse(can_rotate_now())

    def test_can_rotate_now_when_enabled(self):
        """can_rotate_now returns True when ROTATION_ENABLED is True and cooldown elapsed."""
        with patch.object(cfg, "ROTATION_ENABLED", True), \
             patch.object(rotation_mod, "ROTATION_ENABLED", True), \
             patch.object(rotation_mod, "_last_rotation_time", 0.0), \
             patch("time.time", return_value=50000.0):
            self.assertTrue(can_rotate_now())

    def test_can_rotate_now_cooldown(self):
        """can_rotate_now returns False when within cooldown period."""
        with patch.object(cfg, "ROTATION_ENABLED", True), \
             patch.object(rotation_mod, "ROTATION_ENABLED", True), \
             patch.object(rotation_mod, "_last_rotation_time", 1000.0), \
             patch("time.time", return_value=1100.0):
            self.assertFalse(can_rotate_now())

    def test_rotation_boolean_env_parsing(self):
        """Test explicit boolean environment parsing for ROTATION_ENABLED."""
        from hermes.config import parse_bool_env
        self.assertFalse(parse_bool_env("ROTATION_ENABLED", default=False, env_dict={}))
        self.assertTrue(parse_bool_env("ROTATION_ENABLED", default=False, env_dict={"ROTATION_ENABLED": "true"}))
        self.assertTrue(parse_bool_env("ROTATION_ENABLED", default=False, env_dict={"ROTATION_ENABLED": "1"}))
        self.assertTrue(parse_bool_env("ROTATION_ENABLED", default=False, env_dict={"ROTATION_ENABLED": "yes"}))
        self.assertTrue(parse_bool_env("ROTATION_ENABLED", default=False, env_dict={"ROTATION_ENABLED": "True"}))
        self.assertFalse(parse_bool_env("ROTATION_ENABLED", default=False, env_dict={"ROTATION_ENABLED": "false"}))
        self.assertFalse(parse_bool_env("ROTATION_ENABLED", default=False, env_dict={"ROTATION_ENABLED": "0"}))
        self.assertFalse(parse_bool_env("ROTATION_ENABLED", default=False, env_dict={"ROTATION_ENABLED": ""}))

    def test_dynamic_sizing_conviction(self):
        """High conviction should get larger position sizing than medium conviction."""
        with patch("hermes.api.balance.get_balance", return_value={"usdt": 100.0}):
            size_high = get_dynamic_position_size("AVAX", 10.0, 100.0, confidence="High", score=9)
            size_med = get_dynamic_position_size("AVAX", 10.0, 100.0, confidence="Medium", score=6)

            self.assertGreater(size_high, size_med)
            self.assertGreaterEqual(size_med, cfg.MIN_TRADE_USDT)

    def test_dynamic_sizing_rejects_dust(self):
        """Balances below MIN_TRADE_USDT should return 0.0 to prevent untradable dust."""
        with patch("hermes.api.balance.get_balance", return_value={"usdt": 2.0}):
            size_dust = get_dynamic_position_size("AVAX", 10.0, 2.0, confidence="High", score=9)
            self.assertEqual(size_dust, 0.0)

    @patch("hermes.trading.rotation.prices", {
        "BTC": {"price": 83000.0}, # -2.35% (stagnant)
        "SOL": {"price": 130.0}    # +8.33% (winning runner, must not be cut!)
    })
    @patch("hermes.trading.rotation.get_position_momentum_score")
    @patch("time.time", return_value=50000.0)
    def test_evaluate_sluggish_positions(self, mock_time, mock_momentum):
        """Sluggish position (BTC -2.35%, low score) should be chosen over winning runner (SOL +8.33%)."""
        mock_momentum.side_effect = lambda pair, price: (2, "SELL") if pair == "BTC" else (8, "STRONG_BUY")

        sluggish = evaluate_sluggish_positions("AVAX", candidate_score=9)
        self.assertIsNotNone(sluggish)
        assert sluggish is not None
        self.assertEqual(sluggish["pair"], "BTC")
        self.assertLess(sluggish["pnl_pct"], 0.0)
        self.assertEqual(sluggish["score_delta"], 7)


if __name__ == "__main__":
    unittest.main()
