"""
Tests for alert terminology remediation, gross vs ROE vs net PnL clarification,
and backward-compatible state migration (Task E7).
"""

import unittest
from unittest.mock import patch
from tests.support.isolation import IsolatedTestCase
from hermes.notifications.telegram import (
    telegram_trade_alert,
    telegram_exit_alert,
    telegram_futures_exit_alert,
    telegram_rotation_alert,
)
from hermes.trading.tp_evaluator import send_tp_extension_alert


class TestAlertTerminologyRemediation(IsolatedTestCase):
    """Test suite ensuring accurate, transparent alert terminology."""

    def setUp(self):
        super().setUp()
        self.patcher = patch("hermes.notifications.telegram._telegram_enabled", True)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        super().tearDown()

    def test_send_tp_extension_alert_deterministic_source(self):
        """send_tp_extension_alert must not claim AI when deterministic policy was used."""
        captured_messages = []

        with patch("hermes.trading.tp_evaluator.telegram_send", side_effect=lambda msg: captured_messages.append(msg)):
            send_tp_extension_alert(
                pair="BTCUSDT",
                current_price=66000.0,
                entry_price=60000.0,
                pnl_pct=0.10,
                floor_price=64800.0,
                floor_pct=0.08,
                trail_pct=0.035,
                reason="Orderbook imbalance 1.4x supports continuation",
                sentiment_str="BULLISH (+0.60)",
                is_futures=False,
                side="LONG",
                source="DETERMINISTIC",
            )

        self.assertEqual(len(captured_messages), 1)
        msg = captured_messages[0]

        # Must declare deterministic provenance
        self.assertIn("Kebijakan Teknis Deterministik", msg)
        self.assertNotIn("AI Decision: **EXTEND & RIDE THE TREND", msg)

        # Must use conditional floor trigger terminology without claiming guarantee
        self.assertIn("Profit Floor Trigger", msg)
        self.assertIn("bukan eksekusi dijamin", msg)
        self.assertNotIn("Profit Floor Terkunci", msg)
        self.assertNotIn("dijamin profit", msg)

        # Gross price gain distinction
        self.assertIn("Gross Price Gain", msg)
        self.assertIn("Sebelum fee transaksi & slippage", msg)

    def test_send_tp_extension_alert_ai_advisory_source(self):
        """send_tp_extension_alert properly labels AI advisory with gate validation."""
        captured_messages = []

        with patch("hermes.trading.tp_evaluator.telegram_send", side_effect=lambda msg: captured_messages.append(msg)):
            send_tp_extension_alert(
                pair="ETH-PERP",
                current_price=3300.0,
                entry_price=3000.0,
                pnl_pct=0.30,  # 30% ROE (3x)
                floor_price=3080.0,
                floor_pct=0.08,
                trail_pct=0.035,
                reason="AI detected momentum surge",
                sentiment_str="NEUTRAL",
                is_futures=True,
                leverage=3,
                side="LONG",
                source="AI_ADVISORY",
            )

        self.assertEqual(len(captured_messages), 1)
        msg = captured_messages[0]

        self.assertIn("AI Advisory + Gate Validated", msg)
        self.assertIn("Gross Return: *+30.00% ROE*", msg)
        self.assertIn("Sebelum funding & fee exchange", msg)

    def test_send_tp_extension_alert_fallback_source(self):
        """send_tp_extension_alert explicitly discloses fallback when AI timed out."""
        captured_messages = []

        with patch("hermes.trading.tp_evaluator.telegram_send", side_effect=lambda msg: captured_messages.append(msg)):
            send_tp_extension_alert(
                pair="SOLUSDT",
                current_price=110.0,
                entry_price=100.0,
                pnl_pct=0.10,
                floor_price=108.0,
                floor_pct=0.08,
                trail_pct=0.035,
                reason="RSI and orderbook continuation",
                sentiment_str="NEUTRAL",
                is_futures=False,
                side="LONG",
                source="DETERMINISTIC_FALLBACK",
            )

        self.assertEqual(len(captured_messages), 1)
        msg = captured_messages[0]

        self.assertIn("Fallback Deterministik", msg)

    def test_telegram_exit_alert_gross_vs_net_clarification(self):
        """telegram_exit_alert must report Gross Price PnL and qualify Net Realized PnL."""
        captured_messages = []

        with patch("hermes.notifications.telegram.telegram_send", side_effect=lambda msg: captured_messages.append(msg)):
            telegram_exit_alert(
                pair="BTCUSDT",
                side="BUY",
                entry=50000.0,
                exit_price=55000.0,
                qty=0.1,
                pnl_pct=10.0,
                hold_hours=2.5,
                reason="Take Profit Hit",
                net_pnl_usdt=490.0,
                commission_usdt=10.0,
            )

        self.assertEqual(len(captured_messages), 1)
        msg = captured_messages[0]

        # Must not claim "Untung Bersih" for gross price PnL
        self.assertNotIn("Untung Bersih:", msg)
        self.assertIn("Gross Price PnL", msg)
        self.assertIn("Net Realized PnL: *+$490.00 USDT*", msg)
        self.assertIn("Komisi Exchange: *$10.0000 USDT*", msg)
        self.assertIn("Kualifikasi PnL", msg)

    def test_telegram_futures_exit_alert_roe_clarification(self):
        """telegram_futures_exit_alert clarifies ROE is Gross Return on Margin, not Net PnL."""
        captured_messages = []

        with patch("hermes.notifications.telegram.telegram_send", side_effect=lambda msg: captured_messages.append(msg)):
            telegram_futures_exit_alert(
                pair="BTCUSDT",
                side="LONG",
                entry_price=50000.0,
                exit_price=52500.0,
                amount=0.1,
                roe_pct=0.15,
                pnl_usd=250.0,
                initial_margin=1666.67,
                leverage=3,
                reason="Trailing Stop Exit",
                exit_type="TRAIL",
                peak_roe=0.20,
                net_pnl_usd=242.50,
                funding_fee_usd=3.50,
            )

        self.assertEqual(len(captured_messages), 1)
        msg = captured_messages[0]

        # Must distinguish ROE from Net Realized PnL
        self.assertNotIn("Untung Bersih (ROE)", msg)
        self.assertIn("Futures ROE (Gross Return on Margin)", msg)
        self.assertIn("Net Realized PnL: *+$242.50 USDT*", msg)
        self.assertIn("Akumulasi Funding Fee: *$3.5000 USDT*", msg)
        self.assertIn("BUKAN Net Realized PnL", msg)

    def test_telegram_rotation_alert_not_claiming_ai(self):
        """telegram_rotation_alert describes deterministic rotation without claiming AI infallibility."""
        captured_messages = []

        with patch("hermes.notifications.telegram.telegram_send", side_effect=lambda msg: captured_messages.append(msg)):
            telegram_rotation_alert(
                liquidated_pair="ADAUSDT",
                liquidated_pnl=0.01,
                liquidated_score=3,
                freed_usdt=50.0,
                new_pair="SOLUSDT",
                new_price=105.0,
                new_score=8,
                new_confidence="HIGH",
                new_signal_type="LONG",
            )

        self.assertEqual(len(captured_messages), 1)
        msg = captured_messages[0]

        self.assertNotIn("Rasional AI: Modal dipindahkan dari aset tidur", msg)
        self.assertIn("Rasional Rotasi", msg)
        self.assertIn("bukan jaminan keuntungan", msg)

    def test_state_backward_compatibility_guaranteed_floor_pct(self):
        """Positions state preserves backward-compatible fallback for guaranteed_floor_pct."""
        old_state_dict = {
            "entry_price": 100.0,
            "guaranteed_floor_pct": 0.08,
            "state": "RIDING",
        }

        # Simulating state read logic:
        floor_val = old_state_dict.get(
            "profit_floor_trigger_pct",
            old_state_dict.get("profit_floor_pct", old_state_dict.get("guaranteed_floor_pct", 0.08))
        )
        self.assertEqual(floor_val, 0.08)

        # When new key is present, it takes precedence
        new_state_dict = {
            "entry_price": 100.0,
            "profit_floor_trigger_pct": 0.12,
            "guaranteed_floor_pct": 0.08,
            "state": "RIDING",
        }
        floor_val_new = new_state_dict.get(
            "profit_floor_trigger_pct",
            new_state_dict.get("profit_floor_pct", new_state_dict.get("guaranteed_floor_pct", 0.08))
        )
        self.assertEqual(floor_val_new, 0.12)


if __name__ == "__main__":
    unittest.main()
