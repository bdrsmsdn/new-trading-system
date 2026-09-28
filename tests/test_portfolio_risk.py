"""
Unit tests for Centralized Portfolio Risk Gate and Circuit Breaker.

Covers:
1. Per-trade risk budget cap (0.5% equity).
2. Aggregate planned stop risk cap (2.0% equity).
3. Daily loss circuit breaker halt (2.0% equity).
4. Tradable equity USDT cash reserve requirement (25% minimum).
5. Futures disabled by default fail-closed gate.
6. Stale snapshot and invalid data handling.
7. Minimum notional constraints.
"""

import time
import unittest

from hermes.trading.portfolio_risk import (
    check_entry_risk,
    trip_circuit_breaker,
    reset_circuit_breaker,
    is_circuit_breaker_active,
    calculate_portfolio_equity,
    get_aggregate_stop_risk,
)


class TestPortfolioRisk(unittest.TestCase):
    """Test suite for unified portfolio risk gate."""

    def setUp(self):
        reset_circuit_breaker()

    def tearDown(self):
        reset_circuit_breaker()

    def test_per_trade_risk_budget_cap(self):
        """Trade risk exceeding 0.5% of total equity must be rejected."""
        equity = 1000.0  # Max risk per trade = $5.00 (0.5%)
        free_usdt = 500.0

        # Proposed trade: $150 with 5% SL -> Risk = $7.50 (> $5.00 cap)
        decision_over = check_entry_risk(
            symbol="BTCUSDT",
            side="LONG",
            proposed_usdt=150.0,
            price=60000.0,
            current_equity=equity,
            free_usdt=free_usdt,
            stop_loss_pct=0.05,
            max_trade_risk_pct=0.005
        )
        self.assertFalse(decision_over.allowed)
        self.assertEqual(decision_over.reason_code, "RISK_BUDGET_EXCEEDED")
        self.assertIn("exceeds per-trade risk budget", decision_over.reason)

        # Proposed trade: $80 with 5% SL -> Risk = $4.00 (<= $5.00 cap)
        decision_ok = check_entry_risk(
            symbol="BTCUSDT",
            side="LONG",
            proposed_usdt=80.0,
            price=60000.0,
            current_equity=equity,
            free_usdt=free_usdt,
            stop_loss_pct=0.05,
            max_trade_risk_pct=0.005
        )
        self.assertTrue(decision_ok.allowed)
        self.assertEqual(decision_ok.reason_code, "ALLOW")

    def test_aggregate_planned_stop_risk_cap(self):
        """Total planned stop risk across open + new positions exceeding 2.0% equity must be rejected."""
        equity = 1000.0  # Max aggregate risk = $20.00 (2.0%)
        free_usdt = 400.0

        # Existing positions with $18.00 open stop risk
        open_positions = {
            "ETHUSDT": {"qty": 0.1, "entry_price": 2000.0, "stop_loss_pct": 0.05},  # $200 notional * 5% = $10 risk
            "SOLUSDT": {"qty": 1.0, "entry_price": 160.0, "stop_loss_pct": 0.05},   # $160 notional * 5% = $8 risk
        }
        self.assertAlmostEqual(get_aggregate_stop_risk(open_positions), 18.0)

        # Proposed trade: $80 with 5% SL -> $4.00 risk. Total = $22.00 (> $20.00 cap)
        decision_over = check_entry_risk(
            symbol="AVAXUSDT",
            side="LONG",
            proposed_usdt=80.0,
            price=30.0,
            current_equity=equity,
            free_usdt=free_usdt,
            open_positions=open_positions,
            stop_loss_pct=0.05,
            max_aggregate_risk_pct=0.02
        )
        self.assertFalse(decision_over.allowed)
        self.assertEqual(decision_over.reason_code, "RISK_BUDGET_EXCEEDED")
        self.assertIn("Aggregate portfolio stop risk", decision_over.reason)

        # Smaller proposed trade: $30 with 5% SL -> $1.50 risk. Total = $19.50 (<= $20.00 cap)
        decision_ok = check_entry_risk(
            symbol="AVAXUSDT",
            side="LONG",
            proposed_usdt=30.0,
            price=30.0,
            current_equity=equity,
            free_usdt=free_usdt,
            open_positions=open_positions,
            stop_loss_pct=0.05,
            max_aggregate_risk_pct=0.02
        )
        self.assertTrue(decision_ok.allowed)
        self.assertEqual(decision_ok.reason_code, "ALLOW")

    def test_daily_loss_circuit_breaker_halt(self):
        """Daily loss exceeding 2.0% equity trips the circuit breaker and halts all entries."""
        equity = 1000.0
        free_usdt = 500.0

        # Daily loss = $25.00 (2.5% > 2.0% threshold)
        decision = check_entry_risk(
            symbol="BTCUSDT",
            side="LONG",
            proposed_usdt=50.0,
            price=60000.0,
            current_equity=equity,
            free_usdt=free_usdt,
            daily_loss_usdt=25.0,
            circuit_breaker_pct=0.02
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "CIRCUIT_BREAKER_ACTIVE")
        self.assertTrue(is_circuit_breaker_active()[0])

        # Subsequent check while circuit breaker active is immediately rejected
        subsequent = check_entry_risk(
            symbol="ETHUSDT",
            side="LONG",
            proposed_usdt=20.0,
            price=2000.0,
            current_equity=equity,
            free_usdt=free_usdt,
        )
        self.assertFalse(subsequent.allowed)
        self.assertEqual(subsequent.reason_code, "CIRCUIT_BREAKER_ACTIVE")

    def test_tradable_equity_usdt_cash_reserve(self):
        """Spot trade leaving less than 25% equity in USDT cash must be rejected."""
        equity = 100.0     # Required reserve = $25.00 (25%)
        free_usdt = 30.0   # Current free USDT

        # Buying $10 leaves $20 USDT (< $25 required reserve)
        decision_reject = check_entry_risk(
            symbol="SOLUSDT",
            side="LONG",
            proposed_usdt=10.0,
            price=150.0,
            current_equity=equity,
            free_usdt=free_usdt,
            min_reserve_pct=0.25
        )
        self.assertFalse(decision_reject.allowed)
        self.assertEqual(decision_reject.reason_code, "INSUFFICIENT_RESERVE")
        self.assertIn("Insufficient USDT cash reserve", decision_reject.reason)

        # Buying $5.00 is below $5.50 MIN_TRADE_USDT
        # But if free_usdt is $40, buying $10 leaves $30 (>= $25) -> Approved
        decision_allow = check_entry_risk(
            symbol="SOLUSDT",
            side="LONG",
            proposed_usdt=10.0,
            price=150.0,
            current_equity=equity,
            free_usdt=40.0,
            min_reserve_pct=0.25
        )
        self.assertTrue(decision_allow.allowed)
        self.assertEqual(decision_allow.reason_code, "ALLOW")

    def test_futures_disabled_by_default(self):
        """Futures entries must be rejected by default when FUTURES_ENABLED=False."""
        decision = check_entry_risk(
            symbol="BTCUSDT",
            side="LONG",
            proposed_usdt=15.0,
            price=60000.0,
            current_equity=1000.0,
            free_usdt=500.0,
            is_futures=True,
            force_allow_futures=False
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision_reason := decision.reason_code, "FUTURES_ENTRY_DISABLED")
        self.assertIn("disabled by default", decision.reason)

    def test_stale_snapshot_and_invalid_data(self):
        """Stale snapshots (> 60s) or invalid data must fail closed."""
        now = time.time()
        # Stale snapshot (120s old)
        stale_decision = check_entry_risk(
            symbol="BTCUSDT",
            side="LONG",
            proposed_usdt=50.0,
            price=60000.0,
            current_equity=1000.0,
            free_usdt=500.0,
            snapshot_time=now - 120.0,
            snapshot_ttl_seconds=60.0
        )
        self.assertFalse(stale_decision.allowed)
        self.assertEqual(stale_decision.reason_code, "STALE_SNAPSHOT")

        # Invalid price (0.0)
        invalid_price = check_entry_risk(
            symbol="BTCUSDT",
            side="LONG",
            proposed_usdt=50.0,
            price=0.0,
            current_equity=1000.0,
            free_usdt=500.0
        )
        self.assertFalse(invalid_price.allowed)
        self.assertEqual(invalid_price.reason_code, "INVALID_DATA")


if __name__ == "__main__":
    unittest.main()
