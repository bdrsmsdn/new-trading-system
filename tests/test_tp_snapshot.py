"""
Unit tests for typed, fresh momentum snapshot and TP evaluator adapter.

Validates:
1. OrderbookData dataclass is correctly unpacked directly (no .get crash or swallowed exception).
2. Dataclass 1.5 imbalance survives the adapter unchanged.
3. Base quantity vs quote notional calculation.
4. Strict TTL and staleness checks (stale cache rejected with STALE/UNKNOWN, no neutral fabrication).
5. Crossed/empty/invalid orderbooks rejected.
6. Explicit position side handling (LONG vs SHORT).
7. Cross-market proxy tracking for Futures.
8. Zero live network access enforced by IsolatedTestCase.
"""

import time
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock

from tests.support import IsolatedTestCase
from hermes.api.orderbook import OrderbookData, parse_orderbook, get_orderbook, _orderbook_cache
from hermes.trading.momentum_snapshot import (
    MomentumSnapshot,
    Provenance,
    TimeframeSeries,
    build_momentum_snapshot,
    canonicalize_symbol,
    is_continuation_eligible,
    to_decimal_str,
)
from hermes.trading.tp_evaluator import evaluate_tp_momentum, send_tp_extension_alert


class TestTpSnapshotAndAdapter(IsolatedTestCase):
    """Test suite for typed momentum snapshots and TP evaluator adapter."""

    def setUp(self):
        super().setUp()
        _orderbook_cache.clear()

    def _make_sample_orderbook_data(
        self,
        imbalance: float = 1.5,
        bid_vol: float = 1500.0,
        ask_vol: float = 1000.0,
        best_bid: float = 100.0,
        best_ask: float = 100.5,
        ts: float = None,
    ) -> OrderbookData:
        """Helper to construct OrderbookData dataclass."""
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

    def test_orderbook_dataclass_unpacking_survives_without_get(self):
        """OrderbookData dataclass with imbalance 1.5 must be unpacked without .get() error."""
        ob_data = self._make_sample_orderbook_data(imbalance=1.5, bid_vol=150.0, ask_vol=100.0)

        with patch("hermes.trading.tp_evaluator.get_orderbook", return_value=ob_data), \
             patch("hermes.trading.tp_evaluator.get_rsi", return_value=65.0), \
             patch("hermes.trading.tp_evaluator.get_news_sentiment", return_value={"sentiment": "NEUTRAL", "score": 0.0, "articles": []}):

            evaluation = evaluate_tp_momentum(
                pair="BTCUSDT",
                current_price=110.0,
                entry_price=100.0,
                pnl_pct=0.10,
                is_futures=False,
                side="LONG"
            )

            # Imbalance 1.5 must survive unchanged
            self.assertEqual(evaluation["orderbook_imbalance"], 1.5)
            self.assertEqual(evaluation["action"], "EXTEND_AND_RIDE")
            self.assertIn("1.50x", evaluation["reason"])

    def test_orderbook_none_does_not_fabricate_imbalance(self):
        """When orderbook is None, evaluator must report None or UNKNOWN, not pretend it is 1.0."""
        with patch("hermes.trading.tp_evaluator.get_orderbook", return_value=None), \
             patch("hermes.trading.tp_evaluator.get_rsi", return_value=65.0), \
             patch("hermes.trading.tp_evaluator.get_news_sentiment", return_value={"sentiment": "NEUTRAL", "score": 0.0, "articles": []}):

            evaluation = evaluate_tp_momentum(
                pair="ETHUSDT",
                current_price=3300.0,
                entry_price=3000.0,
                pnl_pct=0.10,
                is_futures=False,
                side="LONG"
            )

            # Orderbook missing -> action should take profit, imbalance must not be fabricated
            self.assertIsNone(evaluation["orderbook_imbalance"])
            self.assertEqual(evaluation["action"], "TAKE_PROFIT_NOW")
            self.assertEqual(evaluation.get("orderbook_status"), "UNKNOWN")

    def test_stale_orderbook_rejected(self):
        """Orderbook older than TTL must be treated as STALE and rejected for continuation."""
        stale_ts = time.time() - 120.0  # 120s old (TTL is 60s)
        stale_ob = self._make_sample_orderbook_data(imbalance=2.0, ts=stale_ts)

        with patch("hermes.trading.tp_evaluator.get_orderbook", return_value=stale_ob), \
             patch("hermes.trading.tp_evaluator.get_rsi", return_value=65.0), \
             patch("hermes.trading.tp_evaluator.get_news_sentiment", return_value={"sentiment": "NEUTRAL", "score": 0.0, "articles": []}):

            evaluation = evaluate_tp_momentum(
                pair="SOLUSDT",
                current_price=165.0,
                entry_price=150.0,
                pnl_pct=0.10,
                is_futures=False,
                side="LONG"
            )

            # Even with 2.0 imbalance, stale book must be rejected -> TAKE_PROFIT_NOW
            self.assertEqual(evaluation["action"], "TAKE_PROFIT_NOW")
            self.assertEqual(evaluation.get("orderbook_status"), "STALE")

    def test_crossed_orderbook_rejected_as_invalid(self):
        """Crossed orderbook (best_bid >= best_ask) must be rejected with INVALID provenance."""
        # best_bid 101.0 >= best_ask 100.0
        crossed_ob = self._make_sample_orderbook_data(
            best_bid=101.0,
            best_ask=100.0,
            imbalance=2.0
        )

        snapshot = build_momentum_snapshot(
            symbol="BTCUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=crossed_ob,
            rsi_3m=60.0,
            rsi_1h=55.0,
            position_lifecycle_id="pos_123",
            position_version=1
        )

        self.assertEqual(snapshot.orderbook_provenance.status, "INVALID")
        self.assertEqual(snapshot.orderbook_provenance.reason_code, "CROSSED_ORDERBOOK")
        eligible, reason = is_continuation_eligible(snapshot)
        self.assertFalse(eligible)
        self.assertEqual(reason, "CROSSED_ORDERBOOK")

    def test_missing_rsi_marked_unknown_not_neutral_50(self):
        """Missing or None RSI must be marked UNKNOWN with values=None, not fabricated 50.0."""
        snapshot = build_momentum_snapshot(
            symbol="DOGEUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=self._make_sample_orderbook_data(),
            rsi_3m=None,
            rsi_1h=None,
            position_lifecycle_id="pos_456",
            position_version=1
        )

        rsi_3m_series = next(s for s in snapshot.timeframe_series if s.timeframe == "3m")
        rsi_1h_series = next(s for s in snapshot.timeframe_series if s.timeframe == "1h")

        self.assertEqual(rsi_3m_series.provenance.status, "UNKNOWN")
        self.assertIsNone(rsi_3m_series.values)
        self.assertEqual(rsi_1h_series.provenance.status, "UNKNOWN")
        self.assertIsNone(rsi_1h_series.values)

        eligible, reason = is_continuation_eligible(snapshot)
        self.assertFalse(eligible)
        self.assertEqual(reason, "UNKNOWN_RSI_DATA")

    def test_quote_notional_vs_base_quantity_distinction(self):
        """Base quantity (e.g. 10,000 DOGE) must not be confused with quote notional ($1,500 USDT)."""
        # 10,000 DOGE at $0.15 = 1,500 USDT
        ob = self._make_sample_orderbook_data(
            imbalance=1.5,
            bid_vol=10000.0,
            ask_vol=6666.67,
            best_bid=0.15,
            best_ask=0.1501
        )

        snapshot = build_momentum_snapshot(
            symbol="DOGEUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=ob,
            rsi_3m=65.0,
            rsi_1h=58.0,
            position_lifecycle_id="pos_789",
            position_version=1
        )

        self.assertIsNotNone(snapshot.bid_base_qty)
        self.assertEqual(Decimal(str(snapshot.bid_base_qty)), Decimal("10000.0"))
        self.assertIsNotNone(snapshot.bid_quote_notional)
        # Quote notional around 1500.0, not 10000.0
        bid_notional_dec = Decimal(str(snapshot.bid_quote_notional))
        self.assertTrue(Decimal("1400.0") <= bid_notional_dec <= Decimal("1600.0"))

    def test_side_explicit_handling_long_vs_short(self):
        """Momentum snapshot and evaluator must explicitly accept and record side."""
        ob = self._make_sample_orderbook_data(imbalance=1.5)

        snap_long = build_momentum_snapshot(
            symbol="BTCUSDT",
            venue="FUTURES",
            side="LONG",
            orderbook=ob,
            rsi_3m=70.0,
            rsi_1h=65.0,
            position_lifecycle_id="pos_long",
            position_version=1
        )
        self.assertEqual(snap_long.side, "LONG")
        self.assertEqual(snap_long.venue, "FUTURES")

        snap_short = build_momentum_snapshot(
            symbol="BTCUSDT",
            venue="FUTURES",
            side="SHORT",
            orderbook=ob,
            rsi_3m=30.0,
            rsi_1h=35.0,
            position_lifecycle_id="pos_short",
            position_version=1
        )
        self.assertEqual(snap_short.side, "SHORT")
        self.assertEqual(snap_short.venue, "FUTURES")

    def test_futures_cross_market_proxy_flag(self):
        """Futures snapshot using Spot orderbook must declare cross_market_proxy=True and proxy_venue='SPOT'."""
        ob = self._make_sample_orderbook_data(imbalance=1.5)

        snapshot = build_momentum_snapshot(
            symbol="ETHUSDT",
            venue="FUTURES",
            side="LONG",
            orderbook=ob,
            orderbook_source_venue="SPOT",
            rsi_3m=65.0,
            rsi_1h=60.0,
            position_lifecycle_id="pos_eth",
            position_version=1
        )

        self.assertTrue(snapshot.cross_market_proxy)
        self.assertEqual(snapshot.proxy_venue, "SPOT")

    def test_snapshot_immutability(self):
        """MomentumSnapshot must be frozen and immutable."""
        ob = self._make_sample_orderbook_data(imbalance=1.5)
        snapshot = build_momentum_snapshot(
            symbol="BTCUSDT",
            venue="SPOT",
            side="LONG",
            orderbook=ob,
            rsi_3m=65.0,
            rsi_1h=60.0,
            position_lifecycle_id="pos_freeze",
            position_version=1
        )

        with self.assertRaises((AttributeError, TypeError)):
            snapshot.symbol = "ETHUSDT"  # type: ignore

    def test_canonicalize_symbol(self):
        """canonicalize_symbol strips separators and formats upper BTCUSDT."""
        self.assertEqual(canonicalize_symbol("btc"), "BTCUSDT")
        self.assertEqual(canonicalize_symbol("BTCUSDT"), "BTCUSDT")
        self.assertEqual(canonicalize_symbol("ETH-PERP"), "ETHUSDT")
        self.assertEqual(canonicalize_symbol("sol/usdt"), "SOLUSDT")


if __name__ == "__main__":
    unittest.main()
