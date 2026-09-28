"""
Unit tests for Pure Deterministic Continuation Policy & Directional Symmetry.

Validates:
1. Directional symmetry:
   - Bullish structure supports LONG continuation, rejects SHORT continuation.
   - Bearish structure supports SHORT continuation, rejects LONG continuation.
2. Hard emergency veto:
   - Emergency news/sentiment overrides both deterministic continuation and AI EXTEND.
3. Data-quality fail-closed:
   - Stale/missing/crossed orderbook or missing RSI immediately falls back to TAKE_PROFIT_NOW.
4. Monetary & stop invariants:
   - AI and evaluator cannot alter profit floor trigger (+8.0% / 0.08) or widen stops beyond bounds.
"""

import time
import unittest
from decimal import Decimal
from typing import Optional
from unittest.mock import patch

from tests.support import IsolatedTestCase
from hermes.api.orderbook import OrderbookData
from hermes.trading.momentum_snapshot import (
    MomentumSnapshot,
    build_momentum_snapshot,
)
from hermes.trading.continuation_policy import (
    ContinuationAction,
    ContinuationDecision,
    decide_continuation,
    evaluate_deterministic_continuation,
    validate_ai_advisory_payload,
    DEFAULT_PROFIT_FLOOR_PCT,
    DEFAULT_TRAIL_PCT,
    MIN_TRAIL_PCT,
    MAX_TRAIL_PCT,
)


class TestContinuationPolicy(IsolatedTestCase):
    """Test suite for pure deterministic continuation policy."""

    def _make_sample_orderbook(
        self,
        imbalance: float,
        bid_vol: float = 1000.0,
        ask_vol: float = 1000.0,
        best_bid: float = 100.0,
        best_ask: float = 100.5,
        ts: Optional[float] = None,
    ) -> OrderbookData:
        now = time.time() if ts is None else ts
        bids = [[best_bid, bid_vol * 0.6], [best_bid * 0.999, bid_vol * 0.4]]
        asks = [[best_ask, ask_vol * 0.6], [best_ask * 1.001, ask_vol * 0.4]]
        spread = best_ask - best_bid
        spread_pct = (spread / ((best_bid + best_ask) / 2)) * 100
        bid_notional = sum(p * v for p, v in bids)
        ask_notional = sum(p * v for p, v in asks)
        return OrderbookData(
            bids=bids,
            asks=asks,
            bid_volume=bid_vol,
            ask_volume=ask_vol,
            imbalance=imbalance,
            spread=spread,
            spread_pct=spread_pct,
            thick_bid_level=best_bid,
            thick_ask_level=best_ask,
            ts=now,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
        )

    def test_directional_symmetry_bullish_favors_long_rejects_short(self):
        """Bullish market (imbalance 1.45, RSI 65) extends LONG, but takes profit on SHORT."""
        ob = self._make_sample_orderbook(imbalance=1.45, bid_vol=1450.0, ask_vol=1000.0)

        # LONG snapshot
        snap_long = build_momentum_snapshot(
            symbol="BTCUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=ob,
            rsi_3m=65.0,
            rsi_1h=60.0,
        )
        dec_long = decide_continuation(snap_long, sentiment_data={"sentiment": "BULLISH", "score": 0.5})
        self.assertEqual(dec_long.action, "EXTEND_AND_RIDE")
        self.assertTrue(dec_long.gate_passed)
        self.assertFalse(dec_long.veto_applied)
        self.assertEqual(dec_long.profit_floor_trigger_pct, DEFAULT_PROFIT_FLOOR_PCT)

        # SHORT snapshot with same bullish market
        snap_short = build_momentum_snapshot(
            symbol="BTCUSDT",
            venue="FUTURES",
            side="SHORT",
            orderbook=ob,
            rsi_3m=65.0,
            rsi_1h=60.0,
        )
        dec_short = decide_continuation(snap_short, sentiment_data={"sentiment": "BULLISH", "score": 0.5})
        self.assertEqual(dec_short.action, "TAKE_PROFIT_NOW")
        self.assertIn("Tekanan jual SHORT mulai melemah", dec_short.reason)

    def test_directional_symmetry_bearish_favors_short_rejects_long(self):
        """Bearish market (imbalance 0.60, RSI 35) extends SHORT, but takes profit on LONG."""
        ob = self._make_sample_orderbook(imbalance=0.60, bid_vol=600.0, ask_vol=1000.0)

        # SHORT snapshot
        snap_short = build_momentum_snapshot(
            symbol="ETHUSDT",
            venue="FUTURES",
            side="SHORT",
            orderbook=ob,
            rsi_3m=35.0,
            rsi_1h=40.0,
        )
        dec_short = decide_continuation(snap_short, sentiment_data={"sentiment": "BEARISH", "score": -0.4})
        self.assertEqual(dec_short.action, "EXTEND_AND_RIDE")
        self.assertTrue(dec_short.gate_passed)
        self.assertFalse(dec_short.veto_applied)
        self.assertEqual(dec_short.profit_floor_trigger_pct, DEFAULT_PROFIT_FLOOR_PCT)

        # LONG snapshot with same bearish market
        snap_long = build_momentum_snapshot(
            symbol="ETHUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=ob,
            rsi_3m=35.0,
            rsi_1h=40.0,
        )
        dec_long = decide_continuation(snap_long, sentiment_data={"sentiment": "BEARISH", "score": -0.4})
        self.assertEqual(dec_long.action, "TAKE_PROFIT_NOW")
        self.assertIn("Momentum beli mulai seimbang/menurun", dec_long.reason)

    def test_emergency_news_veto_overrides_continuation_and_ai(self):
        """Emergency sentiment flag immediately vetoes extension even if AI suggests EXTEND."""
        ob = self._make_sample_orderbook(imbalance=1.60)
        snap_long = build_momentum_snapshot(
            symbol="SOLUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=ob,
            rsi_3m=70.0,
            rsi_1h=65.0,
        )

        emergency_sentiment = {
            "sentiment": "BEARISH",
            "score": -0.9,
            "is_emergency": True,
            "articles": [{"title": "Major Exchange Exploit Alert"}]
        }

        # AI suggesting EXTEND
        ai_payload = {
            "action": "EXTEND_AND_RIDE",
            "confidence": "HIGH",
            "recommended_trail_pct": 0.04,
            "reason": "Momentum looks strong despite news."
        }

        decision = decide_continuation(
            snapshot=snap_long,
            sentiment_data=emergency_sentiment,
            ai_advisory=ai_payload
        )

        # Must be vetoed to TAKE_PROFIT_NOW
        self.assertEqual(decision.action, "TAKE_PROFIT_NOW")
        self.assertFalse(decision.gate_passed)
        self.assertTrue(decision.veto_applied)
        self.assertEqual(decision.veto_reason, "EMERGENCY_NEWS_VETO")
        self.assertEqual(decision.source, "EMERGENCY_VETO")

    def test_data_quality_fail_closed_on_stale_data(self):
        """Stale or invalid snapshot immediately fails closed to TAKE_PROFIT_NOW."""
        stale_ob = self._make_sample_orderbook(imbalance=2.0, ts=time.time() - 100)
        snap = build_momentum_snapshot(
            symbol="BTCUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=stale_ob,
            rsi_3m=65.0,
            rsi_1h=60.0,
        )

        ai_payload = {
            "action": "EXTEND_AND_RIDE",
            "confidence": "HIGH",
            "recommended_trail_pct": 0.035,
            "reason": "AI wants to ride."
        }

        decision = decide_continuation(
            snapshot=snap,
            sentiment_data={"sentiment": "NEUTRAL", "score": 0.0},
            ai_advisory=ai_payload
        )

        self.assertEqual(decision.action, "TAKE_PROFIT_NOW")
        self.assertFalse(decision.gate_passed)
        self.assertEqual(decision.source, "DATA_QUALITY_FAIL_CLOSED")

    def test_profit_floor_and_trail_invariants(self):
        """Profit floor trigger is fixed at 0.08 and trail cannot be widened beyond safe boundaries."""
        ob = self._make_sample_orderbook(imbalance=1.5)
        snap = build_momentum_snapshot(
            symbol="BTCUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=ob,
            rsi_3m=65.0,
            rsi_1h=60.0,
        )

        # AI attempts to widen trail to 50% or set negative floor
        ai_payload = {
            "action": "EXTEND_AND_RIDE",
            "confidence": "HIGH",
            "recommended_trail_pct": 0.50,  # Unsafe wide trail
            "guaranteed_floor_pct": 0.01,   # Attempt to lower floor
            "reason": "AI trying to widen trail."
        }

        decision = decide_continuation(
            snapshot=snap,
            sentiment_data={"sentiment": "NEUTRAL", "score": 0.0},
            ai_advisory=ai_payload
        )

        # Floor must remain strictly 0.08 (+8%)
        self.assertEqual(decision.profit_floor_trigger_pct, DEFAULT_PROFIT_FLOOR_PCT)
        self.assertEqual(decision.guaranteed_floor_pct, DEFAULT_PROFIT_FLOOR_PCT)
        # Trail must be clamped to MAX_TRAIL_PCT (0.08) or fallback default (0.035)
        self.assertLessEqual(decision.recommended_trail_pct, MAX_TRAIL_PCT)
        self.assertGreaterEqual(decision.recommended_trail_pct, MIN_TRAIL_PCT)


if __name__ == "__main__":
    unittest.main()
