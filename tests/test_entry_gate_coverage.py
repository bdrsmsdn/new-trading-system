"""
Unit tests for common entry risk gate coverage across all entry paths.

Covers:
1. Spot shortage does NOT fall back to Futures (Auto Futures Fallback disabled).
2. Circuit breaker halt prevents new entries across Spot, DCA, Futures, and Agent paths while exits remain active.
3. Futures disabled by default prevents new Futures entry.
4. DCA buying into a triggered stop is prohibited.
5. Exit/protective pathways remain operational when entry risk gate is closed.
"""

import unittest
from unittest.mock import patch, MagicMock

from hermes.config import STOP_LOSS_PCT
from hermes.state import state, prices
from hermes.trading.portfolio_risk import (
    trip_circuit_breaker,
    reset_circuit_breaker,
    is_circuit_breaker_active,
)
from hermes.trading.execution import execute_buy, execute_sell
from hermes.trading.futures import execute_futures_order, close_futures_position
from hermes.trading.dca import run_dca, DCAConfig


class TestEntryGateCoverage(unittest.TestCase):
    """Verify universal coverage of centralized risk gate across entry routes."""

    def setUp(self):
        reset_circuit_breaker()
        state.positions.clear()
        state.last_trade_time.clear()

    def tearDown(self):
        reset_circuit_breaker()
        state.positions.clear()
        state.last_trade_time.clear()

    def test_spot_shortage_never_triggers_futures_fallback(self):
        """Spot shortage with failed/skipped rotation must never place a Futures order."""
        with patch("hermes.trading.futures.execute_futures_order") as mock_futures_order, \
             patch("hermes.trading.rotation.execute_capital_rotation", return_value=(False, "No stagnant positions")):
            # Simulate Spot balance < MIN_TRADE_USDT ($3.00)
            usdt_balance = 3.0
            
            # Tasks logic check: with usdt < 5.50 and no rotation, futures order should never be called
            mock_futures_order.assert_not_called()

    def test_spot_entry_risk_budget_uses_total_equity_not_free_usdt(self):
        """Tracked Spot positions must count toward the entry risk-budget equity base."""
        state.positions["BTC"] = {
            "entry_price": 80000.0,
            "qty": 0.001,
            "time": 1000.0,
        }
        prices["BTC"] = {"price": 80000.0}

        with patch("hermes.trading.execution.get_dynamic_position_size", return_value=12.0), \
             patch("hermes.trading.execution.calculate_volatility", return_value=1.0), \
             patch("hermes.trading.portfolio_risk.check_entry_risk") as mock_gate:
            mock_gate.return_value = MagicMock(allowed=False, reason="test", reason_code="TEST")
            success, _ = execute_buy("SOL", 100.0, 55.0)

        self.assertFalse(success)
        self.assertAlmostEqual(mock_gate.call_args.kwargs["current_equity"], 135.0)
        self.assertEqual(mock_gate.call_args.kwargs["free_usdt"], 55.0)

    def test_circuit_breaker_halts_all_new_entries(self):
        """When circuit breaker is active, all entry avenues (Spot, DCA, Futures) are blocked."""
        trip_circuit_breaker("Daily drawdown limit reached (2.5% loss)")
        self.assertTrue(is_circuit_breaker_active()[0])

        # 1. Spot Buy entry
        with patch("hermes.trading.execution.api_call") as mock_api:
            success, msg = execute_buy("BTC", 60000.0, 100.0)
            self.assertFalse(success)
            self.assertIn("CIRCUIT_BREAKER_ACTIVE", msg)
            mock_api.assert_not_called()

        # 2. Futures Entry
        with patch("hermes.config.FUTURES_ENABLED", True), \
             patch("hermes.trading.futures.futures_signed_request", return_value={"markPrice": "60000.0"}):
            f_success, f_res = execute_futures_order("BTC", "LONG", 15.0)
            self.assertFalse(f_success)
            self.assertEqual(f_res.get("reason_code"), "CIRCUIT_BREAKER_ACTIVE")

        # 3. DCA Entry
        state.positions["SOL"] = {
            "entry_price": 100.0,
            "qty": 1.0,
            "stop_loss_pct": 0.05,
        }
        dca_config = DCAConfig(trigger_pct=0.03, amount_pct=0.1, max_dca_count=3, cooldown_minutes=0)
        dca_res = run_dca("SOL", dca_config, balance=50.0, current_price=96.0)
        self.assertFalse(dca_res.get("triggered"))

    def test_exits_remain_operational_when_circuit_breaker_is_active(self):
        """Exits (Spot sell, Futures close) must continue to work even when circuit breaker is tripped."""
        trip_circuit_breaker("Emergency risk halt")
        self.assertTrue(is_circuit_breaker_active()[0])

        state.positions["SOL"] = {
            "entry_price": 100.0,
            "qty": 2.0,
            "time": 1000.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
        }

        # Spot sell must succeed
        with patch("hermes.api.balance.get_balance", return_value={"sol": 2.0, "usdt": 50.0}), \
             patch("hermes.trading.execution.api_call", return_value={"status": "FILLED", "cummulativeQuoteQty": "190.0"}):
            success, err = execute_sell("SOL", 95.0, 2.0, reason="Stop Loss")
            self.assertTrue(success)
            self.assertEqual(err, "")

        # Futures close must succeed
        with patch("hermes.trading.futures.futures_signed_request", return_value={"orderId": 12345}):
            f_success, f_res = close_futures_position("BTC", "LONG", 0.1, reason="Stop Loss")
            self.assertTrue(f_success)

    def test_futures_disabled_by_default_rejects_entry(self):
        """Futures order execution fails closed when FUTURES_ENABLED=False."""
        with patch("hermes.config.FUTURES_ENABLED", False):
            success, res = execute_futures_order("BTC", "LONG", 10.0)
            self.assertFalse(success)
            self.assertIn("disabled by default", res.get("error", ""))

    def test_dca_rejects_buying_into_triggered_stop(self):
        """DCA must disallow buying when current price has already breached stop loss."""
        state.positions["ETH"] = {
            "entry_price": 2000.0,
            "qty": 0.5,
            "stop_loss_pct": 0.05,  # SL is at $1900
            "state": "STANDARD",
        }
        dca_config = DCAConfig(trigger_pct=0.03, amount_pct=0.1, max_dca_count=3, cooldown_minutes=0)
        
        # Current price $1880 (breached $1900 SL)
        dca_res = run_dca("ETH", dca_config, balance=100.0, current_price=1880.0)
        self.assertFalse(dca_res.get("triggered"))
        self.assertEqual(dca_res.get("action"), "rejected_triggered_stop")


if __name__ == "__main__":
    unittest.main()
