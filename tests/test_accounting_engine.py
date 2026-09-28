"""Tests for Phase 2 FIFO accounting engine, fill ingestion, fee valuation, and snapshots."""

from decimal import Decimal
import os
import unittest

from hermes.accounting.contracts import (
    AccountingCutover,
    Completeness,
    CutoverStatus,
    DecimalString,
    FillEvent,
    FillKey,
    RealizedOutcome,
    ReconciliationStatus,
    TradeSide,
    ValuationStatus,
    Venue,
    canonical_decimal,
)
from hermes.accounting.ingestion import (
    ingest_binance_trades,
    parse_binance_fill,
    reconcile_and_allocate_fills,
)
from hermes.accounting.repository import (
    DuplicateFillConflictError,
    SqliteAccountingRepository,
)
from hermes.accounting.schema import init_db
from tests.support.isolation import IsolatedTestCase


class TestAccountingEngine(IsolatedTestCase):
    """Test suite for FIFO accounting engine and fill ingestion."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "ledger_test.db"
        init_db(self.db_path)
        self.repo = SqliteAccountingRepository(self.db_path)

        # Create approved cutover baseline
        now_ms = 1700000000000
        cutover = AccountingCutover(
            cutover_id="cutover_test_1",
            account_id="default",
            venue=Venue.SPOT,
            cutover_at_ms=now_ms - 100000,
            baseline_reference="genesis",
            backfill_from_ms=None,
            backfill_through_ms=now_ms - 100000,
            status=CutoverStatus.APPROVED,
            approved_at_ms=now_ms - 100000,
        )
        self.repo.create_cutover(cutover)

    def test_parse_binance_fill_usdt_commission(self) -> None:
        """Parse raw trade with USDT commission into validated FillEvent."""
        raw_trade = {
            "id": 1001,
            "orderId": 5001,
            "symbol": "BTCUSDT",
            "time": 1700000100000,
            "isBuyer": True,
            "price": "50000.00",
            "qty": "0.100000",
            "quoteQty": "5000.00",
            "commission": "5.00",
            "commissionAsset": "USDT",
        }
        fill = parse_binance_fill(raw_trade)
        self.assertEqual(fill.symbol, "BTCUSDT")
        self.assertEqual(fill.side, TradeSide.BUY)
        self.assertEqual(fill.price, "50000")
        self.assertEqual(fill.base_qty, "0.1")
        self.assertEqual(fill.quote_qty, "5000")
        self.assertEqual(fill.commission_usdt, "5")
        self.assertEqual(fill.valuation_status, ValuationStatus.VALUED)

    def test_parse_binance_fill_base_asset_commission(self) -> None:
        """Parse raw trade with base asset commission (e.g. BTC on BTCUSDT)."""
        raw_trade = {
            "id": 1002,
            "orderId": 5002,
            "symbol": "BTCUSDT",
            "time": 1700000100000,
            "isBuyer": True,
            "price": "40000.00",
            "qty": "0.500000",
            "quoteQty": "20000.00",
            "commission": "0.0001",
            "commissionAsset": "BTC",
        }
        fill = parse_binance_fill(raw_trade)
        self.assertEqual(fill.valuation_status, ValuationStatus.VALUED)
        # 0.0001 BTC * 40000 USDT = 4 USDT
        self.assertEqual(fill.commission_usdt, "4")

    def test_parse_binance_fill_unresolved_external_fee(self) -> None:
        """BNB commission without valuation map yields ValuationStatus.UNAVAILABLE."""
        raw_trade = {
            "id": 1003,
            "orderId": 5003,
            "symbol": "BTCUSDT",
            "time": 1700000100000,
            "isBuyer": True,
            "price": "50000.00",
            "qty": "0.100000",
            "commission": "0.01",
            "commissionAsset": "BNB",
        }
        fill = parse_binance_fill(raw_trade, valuation_map=None)
        self.assertEqual(fill.valuation_status, ValuationStatus.UNAVAILABLE)
        self.assertIsNone(fill.commission_usdt)

        # With valuation map
        fill_valued = parse_binance_fill(raw_trade, valuation_map={"BNB": "300.00"})
        self.assertEqual(fill_valued.valuation_status, ValuationStatus.VALUED)
        # 0.01 BNB * 300 = 3 USDT
        self.assertEqual(fill_valued.commission_usdt, "3")

    def test_fifo_exact_multi_buy_partial_sells(self) -> None:
        """FIFO allocates multi-buy lots in exact chronological sequence across partial sells."""
        # Buy 1: 0.1 BTC @ $50,000 (Cost: $5000, Fee: $5)
        buy1 = parse_binance_fill({
            "id": 101, "orderId": 201, "symbol": "BTCUSDT", "time": 1700000010000,
            "isBuyer": True, "price": "50000", "qty": "0.1", "commission": "5", "commissionAsset": "USDT"
        })
        # Buy 2: 0.2 BTC @ $60,000 (Cost: $12000, Fee: $10)
        buy2 = parse_binance_fill({
            "id": 102, "orderId": 202, "symbol": "BTCUSDT", "time": 1700000020000,
            "isBuyer": True, "price": "60000", "qty": "0.2", "commission": "10", "commissionAsset": "USDT"
        })
        self.assertTrue(self.repo.ingest_fill(buy1))
        self.assertTrue(self.repo.ingest_fill(buy2))

        # Sell 1: 0.15 BTC @ $70,000 (Gross proceeds: $10,500, Fee: $10.50)
        # Consumes: all 0.1 of Buy 1 (cost $5000, buy fee $5) + 0.05 of Buy 2 (cost $3000, buy fee $2.50)
        # Total cost basis: $8000, Allocated buy fee: $7.50, Sell fee: $10.50
        # Expected Net PnL = $10,500 - $8,000 - $7.50 - $10.50 = $2,482.00
        sell1 = parse_binance_fill({
            "id": 103, "orderId": 203, "symbol": "BTCUSDT", "time": 1700000030000,
            "isBuyer": False, "price": "70000", "qty": "0.15", "commission": "10.5", "commissionAsset": "USDT"
        })
        self.assertTrue(self.repo.ingest_fill(sell1))
        outcome1 = self.repo.apply_fifo_sell(sell1.key)

        self.assertEqual(outcome1.completeness, Completeness.VERIFIED)
        self.assertEqual(outcome1.sold_base_qty, "0.15")
        self.assertEqual(outcome1.gross_proceeds_usdt, "10500")
        self.assertEqual(outcome1.fifo_cost_usdt, "8000")
        self.assertEqual(outcome1.buy_fee_usdt, "7.5")
        self.assertEqual(outcome1.sell_fee_usdt, "10.5")
        self.assertEqual(outcome1.net_pnl_usdt, "2482")
        self.assertEqual(len(outcome1.allocations), 2)

    def test_net_realized_loss_accounting(self) -> None:
        """Realized losses must be recognized as signed negative outcomes and carry forward."""
        # Buy: 1.0 ETH @ $3000 (Cost: $3000, Fee: $3)
        buy = parse_binance_fill({
            "id": 201, "orderId": 301, "symbol": "ETHUSDT", "time": 1700000010000,
            "isBuyer": True, "price": "3000", "qty": "1.0", "commission": "3", "commissionAsset": "USDT"
        })
        self.assertTrue(self.repo.ingest_fill(buy))

        # Sell: 1.0 ETH @ $2500 (Gross proceeds: $2500, Fee: $2.50)
        # Net PnL = $2500 - $3000 - $3.00 - $2.50 = -$505.50
        sell = parse_binance_fill({
            "id": 202, "orderId": 302, "symbol": "ETHUSDT", "time": 1700000020000,
            "isBuyer": False, "price": "2500", "qty": "1.0", "commission": "2.5", "commissionAsset": "USDT"
        })
        self.assertTrue(self.repo.ingest_fill(sell))
        outcome = self.repo.apply_fifo_sell(sell.key)

        self.assertEqual(outcome.completeness, Completeness.VERIFIED)
        self.assertEqual(outcome.net_pnl_usdt, "-505.5")

        # Check snapshot reflects the negative cumulative net realized PnL
        snapshot = self.repo.distribution_snapshot("default", observed_at_ms=1700000030000)
        self.assertEqual(snapshot.cumulative_verified_net_pnl_usdt, "-505.5")
        self.assertEqual(snapshot.distribution_surplus_usdt, "-505.5")

    def test_duplicate_and_conflicting_fill_handling(self) -> None:
        """Identical duplicate fill is a no-op; conflicting duplicate is quarantined."""
        fill1 = parse_binance_fill({
            "id": 501, "orderId": 601, "symbol": "SOLUSDT", "time": 1700000010000,
            "isBuyer": True, "price": "100", "qty": "5", "commission": "0.5", "commissionAsset": "USDT"
        })
        # First insertion succeeds
        self.assertTrue(self.repo.ingest_fill(fill1))
        # Identical re-insertion returns False (no-op)
        self.assertFalse(self.repo.ingest_fill(fill1))

        # Conflicting payload for same (account, venue, symbol, trade_id)
        conflicting_fill = parse_binance_fill({
            "id": 501, "orderId": 601, "symbol": "SOLUSDT", "time": 1700000010000,
            "isBuyer": True, "price": "120", "qty": "5", "commission": "0.5", "commissionAsset": "USDT"
        })
        with self.assertRaises(DuplicateFillConflictError):
            self.repo.ingest_fill(conflicting_fill)

        # Snapshot should now be UNRESOLVED due to quarantined fill / blocker
        snapshot = self.repo.distribution_snapshot("default", observed_at_ms=1700000020000)
        self.assertEqual(snapshot.completeness, Completeness.UNRESOLVED)
        self.assertIn("CONFLICTING_PAYLOAD_HASH", snapshot.unresolved_reason_codes)


if __name__ == "__main__":
    unittest.main()
