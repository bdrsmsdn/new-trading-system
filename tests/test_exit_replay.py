"""
Unit & Integration Tests for Exit Policy Offline Replay & Ablation Study (X0 / X1 / X2).

Tests:
1. X0 fixed TP/SL + working pre-TP trailing behavior.
2. X1 deterministic +10% extension and floor ratchets (+8%, +15%, +25%, +35%).
3. X2 candidate confirmed-reversal early exit (2-bar structure break vs healthy trend hold).
4. Futures SHORT position directional symmetry across all variants.
5. Metric precision invariants: MFE, MAE, Realized MFE share, Giveback, Fees, Slippage, Drawdown.
6. Multi-regime benchmark ablation execution & deterministic reproducibility.
"""

import math
import unittest
from decimal import Decimal

from hermes.research.exit_replay import (
    ExitReplayEngine,
    MarketBar,
    ReplayTrade,
    AblationSummary,
    create_mega_trend_series,
    create_flash_reversal_series,
    create_early_reversal_series,
    create_healthy_trend_noise_series,
    create_choppy_whipsaw_series,
    create_benchmark_ablation_dataset,
)


class TestExitPolicyReplay(unittest.TestCase):
    """Offline Replay test suite for X0, X1, and X2 exit policies."""

    def setUp(self):
        self.engine = ExitReplayEngine(
            initial_capital_usdt=1000.0,
            slot_size_usdt=100.0,
            fee_rate_per_side=0.001,
            slippage_rate_per_side=0.0005,
            stop_loss_pct=0.05,
            trailing_activation_pct=0.06,
            trailing_stop_pct=0.025,
            checkpoint_pct=0.10,
            initial_profit_floor_pct=0.08,
            reversal_threshold_pct=0.02,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 1. X0 Policy Tests
    # ─────────────────────────────────────────────────────────────────────────

    def test_x0_hard_stop_loss_hit(self):
        """X0 exits at -5.0% SL when price drops."""
        t0 = 1700000000.0
        bars = [
            MarketBar(timestamp=t0, open=100.0, high=100.5, low=99.8, close=100.0),
            MarketBar(timestamp=t0 + 3600, open=100.0, high=100.2, low=97.0, close=97.5),
            MarketBar(timestamp=t0 + 7200, open=97.5, high=97.8, low=94.5, close=94.8), # breaches 95.0 SL
        ]
        trade = self.engine.simulate_trade("SOLUSDT", "LONG", bars, "X0", 0)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.exit_type, "STOP_LOSS")
        self.assertLess(trade.net_pnl_pct, -0.049) # Approx -5% + fees + slippage
        self.assertEqual(trade.hold_bars, 2)
        self.assertAlmostEqual(trade.mae_pct, -0.055, places=3)

    def test_x0_trailing_stop_pre_tp(self):
        """X0 arms trailing at +6.0% and exits on 2.5% retracement before TP."""
        t0 = 1700000000.0
        bars = [
            MarketBar(timestamp=t0, open=100.0, high=100.5, low=99.8, close=100.0),
            MarketBar(timestamp=t0 + 3600, open=100.0, high=107.0, low=100.0, close=107.0), # Peak +7%
            MarketBar(timestamp=t0 + 7200, open=107.0, high=107.2, low=104.0, close=104.2), # Retraces past 107 * 0.975 = 104.325
        ]
        trade = self.engine.simulate_trade("SOLUSDT", "LONG", bars, "X0", 0)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.exit_type, "TRAILING_STOP")
        self.assertGreater(trade.gross_pnl_pct, 0.04) # Locked in gain > 4%
        self.assertAlmostEqual(trade.mfe_pct, 0.072, places=3)

    def test_x0_caps_take_profit_at_ten_percent(self):
        """X0 exits immediately at +10% TP and does NOT extend even in mega-trend."""
        bars = create_mega_trend_series(100.0, 0.45, 30, "SOLUSDT", "LONG")
        trade = self.engine.simulate_trade("SOLUSDT", "LONG", bars, "X0", 0)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.exit_type, "TAKE_PROFIT_FIXED")
        self.assertAlmostEqual(trade.gross_pnl_pct, 0.10 * (1.0 - 0.0005), places=3)
        self.assertLess(trade.gross_pnl_pct, 0.11)
        self.assertEqual(trade.position_state_at_exit, "STANDARD")

    # ─────────────────────────────────────────────────────────────────────────
    # 2. X1 Policy Tests (Extension & Ratchets)
    # ─────────────────────────────────────────────────────────────────────────

    def test_x1_extends_into_riding_and_captures_runner(self):
        """X1 extends past +10% at checkpoint and rides mega-trend to floor ratchets."""
        bars = create_mega_trend_series(100.0, 0.45, 30, "SOLUSDT", "LONG")
        trade_x0 = self.engine.simulate_trade("SOLUSDT", "LONG", bars, "X0", 0)
        trade_x1 = self.engine.simulate_trade("SOLUSDT", "LONG", bars, "X1", 0)

        self.assertIsNotNone(trade_x1)
        self.assertEqual(trade_x1.position_state_at_exit, "RIDING")
        # X1 captures significantly more return than X0's 10% cap
        self.assertGreater(trade_x1.gross_pnl_pct, 0.30)
        self.assertGreater(trade_x1.gross_pnl_pct, trade_x0.gross_pnl_pct * 3)
        self.assertIn(trade_x1.exit_type, ("PROFIT_FLOOR", "TRAILING_STOP"))

    def test_x1_floor_protection_on_flash_reversal(self):
        """X1 enters RIDING at +10% and stops out at protected +8% floor when price crashes."""
        bars = create_flash_reversal_series(100.0, 0.12, 0.08, "BTCUSDT", "LONG")
        trade_x1 = self.engine.simulate_trade("BTCUSDT", "LONG", bars, "X1", 0)

        self.assertIsNotNone(trade_x1)
        self.assertIn(trade_x1.exit_type, ("PROFIT_FLOOR", "TRAILING_STOP"))
        # Floor protected at ~+8%
        self.assertGreaterEqual(trade_x1.gross_pnl_pct, 0.075)

    def test_x1_ratchets_multiple_tiers(self):
        """X1 accurately ratchets floor at +20% -> 15%, +30% -> 25%, +40% -> 35%."""
        t0 = 1700000000.0
        # Build series reaching +42% then dropping to +34%
        bars = [
            MarketBar(t0, 100.0, 100.5, 99.8, 100.0, rsi=50.0),
            MarketBar(t0 + 3600, 100.0, 111.0, 100.0, 111.0, rsi=65.0, orderbook_imbalance=1.3), # Checkpoint reached
            MarketBar(t0 + 7200, 111.0, 122.0, 110.0, 122.0, rsi=70.0), # +22% -> floor 15% (115.0)
            MarketBar(t0 + 10800, 122.0, 132.0, 121.0, 132.0, rsi=75.0), # +32% -> floor 25% (125.0)
            MarketBar(t0 + 14400, 132.0, 142.0, 131.0, 142.0, rsi=80.0), # +42% -> floor 35% (135.0)
            MarketBar(t0 + 18000, 142.0, 142.0, 134.0, 134.5, rsi=45.0), # low 134.0 breaches protective stop
        ]
        trade = self.engine.simulate_trade("ETHUSDT", "LONG", bars, "X1", 0)
        self.assertIsNotNone(trade)
        self.assertIn(trade.exit_type, ("PROFIT_FLOOR", "TRAILING_STOP"))
        self.assertGreater(trade.gross_pnl_pct, 0.35)

    # ─────────────────────────────────────────────────────────────────────────
    # 3. X2 Policy Tests (Confirmed Reversal Early Exit)
    # ─────────────────────────────────────────────────────────────────────────

    def test_x2_early_exit_on_confirmed_reversal(self):
        """X2 exits at +2.1% on 2-bar adverse structure break, avoiding slide to -5% SL."""
        bars = create_early_reversal_series(100.0, 0.035, "ETHUSDT", "LONG")
        trade_x0 = self.engine.simulate_trade("ETHUSDT", "LONG", bars, "X0", 0)
        trade_x1 = self.engine.simulate_trade("ETHUSDT", "LONG", bars, "X1", 0)
        trade_x2 = self.engine.simulate_trade("ETHUSDT", "LONG", bars, "X2", 0)

        # X0 and X1 didn't reach +6% activation, so they round-tripped into STOP_LOSS -5%
        self.assertEqual(trade_x0.exit_type, "STOP_LOSS")
        self.assertEqual(trade_x1.exit_type, "STOP_LOSS")
        self.assertLess(trade_x0.net_pnl_usdt, -4.0)

        # X2 successfully exited early with positive net PnL!
        self.assertEqual(trade_x2.exit_type, "CONFIRMED_REVERSAL")
        self.assertGreater(trade_x2.net_pnl_usdt, 1.5)
        self.assertGreater(trade_x2.gross_pnl_pct, 0.018)

    def test_x2_holds_healthy_trend_despite_raw_sell_signal(self):
        """X2 ignores raw STRONG_SELL when trend structure is intact and continues to profit."""
        bars = create_healthy_trend_noise_series(100.0, "AVAXUSDT", "LONG")
        trade_x2 = self.engine.simulate_trade("AVAXUSDT", "LONG", bars, "X2", 0)

        self.assertIsNotNone(trade_x2)
        # Did not exit on premature noise
        self.assertNotEqual(trade_x2.exit_type, "CONFIRMED_REVERSAL")
        self.assertGreater(trade_x2.gross_pnl_pct, 0.10)

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Futures SHORT Position Tests
    # ─────────────────────────────────────────────────────────────────────────

    def test_short_symmetry_all_variants(self):
        """Verify directional symmetry for SHORT positions (downward move = profit)."""
        bars_mega = create_mega_trend_series(100.0, 0.35, 25, "BTCUSDT", "SHORT")
        
        trade_x0 = self.engine.simulate_trade("BTCUSDT", "SHORT", bars_mega, "X0", 0)
        trade_x1 = self.engine.simulate_trade("BTCUSDT", "SHORT", bars_mega, "X1", 0)
        trade_x2 = self.engine.simulate_trade("BTCUSDT", "SHORT", bars_mega, "X2", 0)

        self.assertEqual(trade_x0.exit_type, "TAKE_PROFIT_FIXED")
        self.assertAlmostEqual(trade_x0.gross_pnl_pct, 0.10 * (1.0 - 0.0005), places=2)

        self.assertIn(trade_x1.exit_type, ("PROFIT_FLOOR", "TRAILING_STOP"))
        self.assertGreater(trade_x1.gross_pnl_pct, 0.20)

        self.assertIn(trade_x2.exit_type, ("PROFIT_FLOOR", "TRAILING_STOP"))
        self.assertGreater(trade_x2.gross_pnl_pct, 0.20)

    def test_short_early_reversal_exit(self):
        """Verify X2 early exits on bullish reversal in SHORT position."""
        bars_rev = create_early_reversal_series(100.0, 0.035, "ETHUSDT", "SHORT")
        trade_x2 = self.engine.simulate_trade("ETHUSDT", "SHORT", bars_rev, "X2", 0)

        self.assertEqual(trade_x2.exit_type, "CONFIRMED_REVERSAL")
        self.assertGreater(trade_x2.gross_pnl_pct, 0.015)

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Metrics & Calculation Invariants
    # ─────────────────────────────────────────────────────────────────────────

    def test_metrics_accounting_and_giveback(self):
        """Check fee, slippage, net PnL, giveback, and MFE/MAE consistency."""
        t0 = 1700000000.0
        bars = [
            MarketBar(t0, 100.0, 100.0, 99.0, 100.0),
            MarketBar(t0 + 3600, 100.0, 108.0, 99.0, 108.0), # MFE = +8%, Peak = 108, low 99.0 gives MAE = -1%
            MarketBar(t0 + 7200, 108.0, 108.0, 105.0, 105.1), # Trailing stop at 108 * 0.975 = 105.3
        ]
        trade = self.engine.simulate_trade("SOLUSDT", "LONG", bars, "X0", 0)
        self.assertIsNotNone(trade)
        
        # MFE should be at least +8%
        self.assertGreaterEqual(trade.mfe_pct, 0.08)
        # MAE should be -1%
        self.assertAlmostEqual(trade.mae_pct, -0.01, places=3)
        # Giveback should be positive (peak 8% - realized ~5.2%)
        self.assertGreater(trade.giveback_pct, 0.02)
        # Net PnL = Gross PnL - Fees - Slippage
        expected_net = trade.gross_pnl_usdt - trade.fee_usdt
        self.assertAlmostEqual(trade.net_pnl_usdt, expected_net, places=4)

    # ─────────────────────────────────────────────────────────────────────────
    # 6. Benchmark Ablation Study Execution
    # ─────────────────────────────────────────────────────────────────────────

    def test_full_benchmark_ablation_comparison(self):
        """Execute full benchmark ablation and assert comparative invariants."""
        dataset = create_benchmark_ablation_dataset()
        results = self.engine.run_ablation_study(dataset)

        self.assertIn("X0", results)
        self.assertIn("X1", results)
        self.assertIn("X2", results)

        x0 = results["X0"]
        x1 = results["X1"]
        x2 = results["X2"]

        self.assertEqual(x0.total_trades, len(dataset))
        self.assertEqual(x1.total_trades, len(dataset))
        self.assertEqual(x2.total_trades, len(dataset))

        # Comparative Assertions:
        # 1. X1 captures trend extensions better than X0 (higher net PnL & expectancy)
        self.assertGreater(x1.total_net_pnl_usdt, x0.total_net_pnl_usdt)
        self.assertGreater(x1.net_expectancy_pct, x0.net_expectancy_pct)

        # 2. X2 avoids round-trip losses on +2% reversals (higher win rate, higher net PnL, lower max DD)
        self.assertGreaterEqual(x2.win_rate_pct, x1.win_rate_pct)
        self.assertGreater(x2.total_net_pnl_usdt, x1.total_net_pnl_usdt)
        self.assertLessEqual(x2.max_drawdown_pct, x1.max_drawdown_pct)
        self.assertGreater(x2.profit_factor, x1.profit_factor)

        # 3. Exit reasons distribution integrity
        self.assertGreater(x2.exit_reasons.get("CONFIRMED_REVERSAL", 0), 0)
        self.assertEqual(x0.exit_reasons.get("CONFIRMED_REVERSAL", 0), 0)

    def test_deterministic_reproducibility(self):
        """Re-running the ablation study yields 100% bitwise identical metric results."""
        dataset = create_benchmark_ablation_dataset()
        run1 = self.engine.run_ablation_study(dataset)
        run2 = self.engine.run_ablation_study(dataset)

        for variant in ("X0", "X1", "X2"):
            d1 = run1[variant].to_dict()
            d2 = run2[variant].to_dict()
            self.assertEqual(d1, d2)


if __name__ == "__main__":
    unittest.main()
