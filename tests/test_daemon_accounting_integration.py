"""Integration tests for hermes.daemon runtime accounting cutover, trade sync, and disarming.

Covers:
1. Entrypoint startup & periodic sync with mocked Binance API (paginated myTrades, fee valuation, SQLite fills/allocations).
2. SpotToFundingCollector execution feature flagging & PENDING cutover gating.
3. Hard disarming of legacy daily_profit_state.json authorization under all flag states.
4. Shadow rotation cost-aware evaluation with zero order mutations.
5. Restart idempotency, duplicate ingestion rejection, and deterministic isolated execution.
"""

import asyncio
from decimal import Decimal
import json
import sqlite3
import time
from typing import Any, Dict, List
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import hermes.config as cfg
from hermes.accounting.contracts import (
    AccountingCutover,
    CutoverStatus,
    FillEvent,
    FillKey,
    RotationAction,
    TradeSide,
    TransferStatus,
    ValuationStatus,
    Venue,
    canonical_decimal,
)
from hermes.accounting.ingestion import (
    fetch_paginated_binance_trades,
    sync_accounting_trades,
)
from hermes.accounting.repository import (
    SqliteAccountingRepository,
    SqliteRotationRepository,
    SqliteTransferIntentRepository,
)
from hermes.accounting.schema import get_db_connection, init_db
from hermes.api import transfer as transfer_mod
from hermes.api.transfer import run_daily_profit_collector, track_realized_profit
from hermes.daemon.tasks import (
    daemon_periodic_sync,
    daemon_trade_check_v2,
    validate_positions_on_startup,
)
from hermes.state import prices, state
from tests.support.isolation import IsolatedTestCase


class TestDaemonAccountingIntegration(IsolatedTestCase):
    """Full daemon-level integration suite for accounting cutover, sync, and disarming."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "daemon_accounting.db"
        init_db(self.db_path)
        self.patcher_db = patch.object(cfg, "ACCOUNTING_DB_PATH", self.db_path)
        self.patcher_db.start()

        # Reset in-memory state
        state.positions.clear()
        state.active_pairs = ["BTC", "ETH", "SOL"]
        state.fg_value = 50
        state.last_trade_time.clear()

        prices.clear()
        prices["BTC"] = {"price": 60000.0, "change_24h": 1.5, "source": "test"}
        prices["ETH"] = {"price": 3000.0, "change_24h": 0.5, "source": "test"}
        prices["SOL"] = {"price": 100.0, "change_24h": 2.0, "source": "test"}
        prices["BNB"] = {"price": 500.0, "change_24h": 0.0, "source": "test"}

    def tearDown(self) -> None:
        self.patcher_db.stop()
        super().tearDown()

    # -------------------------------------------------------------------------
    # Acceptance Criterion 1: Paginated myTrades sync, fee valuation, and SQLite persistence
    # -------------------------------------------------------------------------

    def test_fetch_paginated_binance_trades_multi_page(self) -> None:
        """Verify fetch_paginated_binance_trades pages correctly through fromId until completion."""
        page1 = [
            {"id": 101, "price": "100.0", "qty": "1.0", "time": 1700000000000, "isBuyer": True},
            {"id": 102, "price": "101.0", "qty": "1.0", "time": 1700000001000, "isBuyer": True},
        ]
        page2 = [
            {"id": 103, "price": "105.0", "qty": "1.0", "time": 1700000002000, "isBuyer": False},
        ]

        def mock_signed_request(endpoint: str, params: Dict[str, Any], method: str = "GET") -> List[Dict[str, Any]]:
            self.assertEqual(endpoint, "/api/v3/myTrades")
            self.assertEqual(method, "GET")
            from_id = params.get("fromId")
            if from_id is None:
                return page1  # Page 1 (limit=2 for test)
            elif from_id == 103:
                return page2  # Page 2
            return []

        with patch("hermes.api.auth.binance_signed_request", side_effect=mock_signed_request):
            trades = fetch_paginated_binance_trades("SOLUSDT", limit=2)

        self.assertEqual(len(trades), 3)
        self.assertEqual([t["id"] for t in trades], [101, 102, 103])

    def test_sync_accounting_trades_fee_valuation_and_fifo_persistence(self) -> None:
        """Verify sync_accounting_trades ingests trades, values fees (USDT, BNB), and allocates FIFO sells."""
        repo = SqliteAccountingRepository(self.db_path)
        now_ms = int(time.time() * 1000)
        repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_genesis",
                account_id="default",
                venue=Venue.SPOT,
                cutover_at_ms=now_ms - 50000,
                baseline_reference="genesis_init",
                backfill_from_ms=None,
                backfill_through_ms=now_ms - 50000,
                status=CutoverStatus.APPROVED,
                approved_at_ms=now_ms - 50000,
            )
        )

        mock_trades = [
            {
                "id": 201,
                "orderId": 501,
                "symbol": "SOLUSDT",
                "price": "100.0",
                "qty": "2.0",
                "quoteQty": "200.0",
                "commission": "0.001",
                "commissionAsset": "BNB",  # Priced at $500 in valuation map -> $0.50 USDT fee
                "time": now_ms - 30000,
                "isBuyer": True,
                "isMaker": False,
            },
            {
                "id": 202,
                "orderId": 502,
                "symbol": "SOLUSDT",
                "price": "110.0",
                "qty": "1.0",
                "quoteQty": "110.0",
                "commission": "0.10",
                "commissionAsset": "USDT",
                "time": now_ms - 10000,
                "isBuyer": False,
                "isMaker": False,
            },
        ]

        with patch("hermes.accounting.ingestion.fetch_paginated_binance_trades", return_value=mock_trades):
            inserted, dups = sync_accounting_trades(
                repo=repo,
                symbols=["SOLUSDT"],
                valuation_map={"BNB": "500.0", "USDT": "1.0"},
                db_path=self.db_path,
            )

        self.assertEqual(inserted, 2)
        self.assertEqual(dups, 0)

        # Verify SQLite fills table
        con = get_db_connection(self.db_path)
        try:
            cur = con.execute("SELECT * FROM fills WHERE symbol = 'SOLUSDT' ORDER BY event_time_ms ASC;")
            fills = cur.fetchall()
            self.assertEqual(len(fills), 2)
            buy_fill = fills[0]
            sell_fill = fills[1]

            self.assertEqual(buy_fill["side"], "BUY")
            self.assertEqual(buy_fill["commission_asset"], "BNB")
            self.assertEqual(buy_fill["commission_qty"], "0.001")
            self.assertEqual(buy_fill["commission_usdt"], "0.5")
            self.assertEqual(buy_fill["valuation_status"], "VALUED")

            self.assertEqual(sell_fill["side"], "SELL")
            self.assertEqual(sell_fill["commission_asset"], "USDT")
            self.assertEqual(sell_fill["commission_usdt"], "0.1")

            # Verify FIFO realized_outcomes table
            cur_out = con.execute("SELECT * FROM realized_outcomes WHERE sell_trade_id = '202';")
            outcomes = cur_out.fetchall()
            self.assertEqual(len(outcomes), 1)
            outcome = outcomes[0]
            self.assertEqual(outcome["sold_base_qty"], "1")
            self.assertEqual(outcome["gross_proceeds_usdt"], "110")
            self.assertEqual(outcome["fifo_cost_usdt"], "100")
            self.assertEqual(outcome["buy_fee_usdt"], "0.25")
            self.assertEqual(outcome["sell_fee_usdt"], "0.1")
            self.assertEqual(outcome["net_pnl_usdt"], "9.65")
            self.assertEqual(outcome["completeness"], "VERIFIED")

            # Verify FIFO lot_allocations table
            cur_alloc = con.execute("SELECT * FROM lot_allocations WHERE closing_trade_id = '202';")
            allocations = cur_alloc.fetchall()
            self.assertEqual(len(allocations), 1)
            alloc = allocations[0]
            self.assertEqual(alloc["allocated_base_qty"], "1")
            self.assertEqual(alloc["allocated_cost_usdt"], "100")
            self.assertEqual(alloc["allocated_buy_fee_usdt"], "0.25")
            self.assertEqual(alloc["allocated_sell_fee_usdt"], "0.1")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # Acceptance Criterion 2: SpotToFundingCollector feature flag and cutover status
    # -------------------------------------------------------------------------

    def test_startup_creates_cutover_in_pending_status_never_self_approved(self) -> None:
        """Validate startup lifecycle initializes AccountingCutover in PENDING status."""
        def mock_get_balance(use_cache: bool = False) -> Dict[str, float]:
            return {"usdt": 100.0, "sol": 0.0}

        with patch("hermes.trading.reconcile.reconcile_positions_from_binance", return_value=[]), \
             patch("hermes.accounting.ingestion.fetch_paginated_binance_trades", return_value=[]):
            validate_positions_on_startup(mock_get_balance)

        con = get_db_connection(self.db_path)
        try:
            cur = con.execute("SELECT * FROM accounting_cutovers WHERE account_id = 'default' AND venue = 'SPOT';")
            cutovers = cur.fetchall()
            self.assertEqual(len(cutovers), 1)
            cutover = cutovers[0]
            self.assertEqual(cutover["cutover_id"], "cutover_genesis")
            self.assertEqual(cutover["status"], "PENDING")
            self.assertIsNone(cutover["approved_at_ms"])
        finally:
            con.close()

    def test_spot_to_funding_collector_blocked_when_flag_disabled(self) -> None:
        """run_daily_profit_collector is disabled when DAILY_PROFIT_COLLECTION is False."""
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", False), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", False), \
             patch("hermes.api.auth.binance_signed_request") as mock_req:
            collected = run_daily_profit_collector()

        self.assertFalse(collected)
        mock_req.assert_not_called()

    def test_spot_to_funding_collector_blocked_when_cutover_pending(self) -> None:
        """run_daily_profit_collector refuses transfer when cutover is PENDING even if flag is True."""
        repo = SqliteAccountingRepository(self.db_path)
        now_ms = int(time.time() * 1000)
        repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_pending",
                account_id="default",
                venue=Venue.SPOT,
                cutover_at_ms=now_ms,
                baseline_reference="init",
                backfill_from_ms=None,
                backfill_through_ms=now_ms,
                status=CutoverStatus.PENDING,
                approved_at_ms=None,
            )
        )

        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 100.0}), \
             patch("hermes.api.auth.binance_signed_request") as mock_req:
            collected = run_daily_profit_collector()

        self.assertFalse(collected)
        mock_req.assert_not_called()

    def test_spot_to_funding_collector_executes_when_approved_and_surplus_exists(self) -> None:
        """run_daily_profit_collector executes transfer when cutover is APPROVED and surplus >= target."""
        repo = SqliteAccountingRepository(self.db_path)
        now_ms = int(time.time() * 1000)
        repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_approved",
                account_id="default",
                venue=Venue.SPOT,
                cutover_at_ms=now_ms - 60000,
                baseline_reference="init",
                backfill_from_ms=None,
                backfill_through_ms=now_ms - 60000,
                status=CutoverStatus.APPROVED,
                approved_at_ms=now_ms - 60000,
            )
        )

        # Ingest $5.00 net realized profit
        repo.ingest_fill(FillEvent(
            schema_version=1, account_id="default", venue=Venue.SPOT, symbol="BTCUSDT",
            trade_id="t_b1", order_id="o1", event_time_ms=now_ms - 40000,
            side=TradeSide.BUY, price="50000", base_qty="0.001", quote_qty="50",
            commission_asset="USDT", commission_qty="0", commission_usdt="0",
            valuation_status=ValuationStatus.VALUED, source_payload_hash="h1"
        ))
        repo.ingest_fill(FillEvent(
            schema_version=1, account_id="default", venue=Venue.SPOT, symbol="BTCUSDT",
            trade_id="t_s1", order_id="o2", event_time_ms=now_ms - 20000,
            side=TradeSide.SELL, price="55000", base_qty="0.001", quote_qty="55",
            commission_asset="USDT", commission_qty="0", commission_usdt="0",
            valuation_status=ValuationStatus.VALUED, source_payload_hash="h2"
        ))
        repo.apply_fifo_sell(FillKey("default", Venue.SPOT, "BTCUSDT", "t_s1"))

        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 50.0}), \
             patch("hermes.api.auth.binance_signed_request", return_value={"tranId": 987654}) as mock_signed, \
             patch.object(transfer_mod, "telegram_send") as mock_tg:
            collected = run_daily_profit_collector()

        self.assertTrue(collected)
        mock_signed.assert_called_once()
        mock_tg.assert_called_once()

        # Check intent table in SQLite
        con = get_db_connection(self.db_path)
        try:
            cur = con.execute("SELECT * FROM transfer_intents WHERE account_id = 'default';")
            intents = cur.fetchall()
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0]["status"], "CONFIRMED")
            self.assertEqual(intents[0]["exchange_tran_id"], "987654")
            self.assertEqual(intents[0]["amount_usdt"], "1")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # Acceptance Criterion 3: Disarming legacy daily_profit_state.json
    # -------------------------------------------------------------------------

    def test_legacy_daily_profit_json_cannot_authorize_transfers_under_any_state(self) -> None:
        """Ensure legacy daily_profit_state.json is disarmed and never authorizes transfers."""
        # Inject huge profit in legacy json tracker
        track_realized_profit("ETH", 500.0)

        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 1000.0}), \
             patch("hermes.api.auth.binance_signed_request") as mock_req:
            # Without SQLite approved ledger surplus, transfer returns False
            result = run_daily_profit_collector()

        self.assertFalse(result)
        mock_req.assert_not_called()

    # -------------------------------------------------------------------------
    # Acceptance Criterion 4: Shadow rotation logs cost-aware decisions with ZERO exchange orders emitted
    # -------------------------------------------------------------------------

    def test_shadow_rotation_evaluates_and_emits_zero_exchange_orders(self) -> None:
        """When Spot USDT is insufficient, daemon evaluates cost-aware shadow rotation without emitting orders."""
        state.positions["ETHUSDT"] = {
            "entry_price": 3000.0,
            "qty": 0.01,
            "time": time.time() - 3600,  # Held for 1h
            "mode": "NORMAL",
            "sl": 2850.0,
            "tp1": 3150.0,
            "tp2": 3300.0,
        }
        state.active_pairs = ["SOL"]
        state.last_trade_time.clear()

        prices["ETHUSDT"] = {"price": 3005.0}  # Small gain (+0.16%)
        prices["SOLUSDT"] = {"price": 100.0}
        prices["SOL"] = {"price": 100.0}
        prices["ETH"] = {"price": 3005.0}

        def mock_get_balance(use_cache: bool = False) -> Dict[str, float]:
            return {"usdt": 2.0, "ETH": 0.01}  # USDT < MIN_TRADE_USDT ($25)

        mock_signal_response = {
            "signal": {
                "symbol": "SOLUSDT",
                "signal_type": "LONG",
                "signal_confidence": "High",
                "score": 9,
                "price": 100.0,
                "rsi_value": 45.0,
                "daily_position": 25.0,
                "stop_loss": 95.0,
                "take_profit_1": 105.0,
                "take_profit_2": 110.0,
            }
        }

        # Mock asyncio.sleep to execute 1 iteration and then cancel
        sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])

        with patch("asyncio.sleep", new=sleep_mock), \
             patch("hermes.daemon.tasks.get_signal_v2_async", new=AsyncMock(return_value=mock_signal_response)), \
             patch("hermes.indicators.news_sentiment.is_news_safe_to_buy", return_value=(True, "SAFE")), \
             patch("hermes.daemon.tasks.check_open_positions"), \
             patch("hermes.trading.futures_monitor.check_open_futures_positions"), \
             patch("hermes.api.auth.binance_signed_request") as mock_binance_req, \
             patch("hermes.trading.rotation.execute_capital_rotation") as mock_legacy_rotation:

            try:
                asyncio.run(daemon_trade_check_v2(mock_get_balance, min_confidence="Medium"))
            except asyncio.CancelledError:
                pass

        # Legacy rotation must NOT be called
        mock_legacy_rotation.assert_not_called()
        # Zero exchange orders must be emitted
        mock_binance_req.assert_not_called()

        # Check SQLite rotation_decisions table for recorded shadow decision
        con = get_db_connection(self.db_path)
        try:
            cur = con.execute("SELECT * FROM rotation_decisions;")
            decisions = cur.fetchall()
            self.assertGreaterEqual(len(decisions), 1)

            dec = decisions[0]
            self.assertTrue(dec["candidate_symbol"].startswith("SOL"))
            self.assertEqual(dec["held_symbol"], "ETHUSDT")
            self.assertEqual(dec["model_version"], "v2")
            self.assertEqual(dec["schema_version"], 1)
            self.assertIsNotNone(dec["score_edge"])
            self.assertIsNotNone(dec["expected_net_benefit_usdt"])
            self.assertEqual(dec["action"], "APPROVE")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # Acceptance Criterion 5: Restart idempotency and isolated execution
    # -------------------------------------------------------------------------

    def test_startup_sync_restart_idempotency_and_duplicate_rejection(self) -> None:
        """Restarting validate_positions_on_startup maintains ledger idempotency and rejects duplicate trades."""
        mock_trades = [
            {
                "id": 9901,
                "orderId": 8801,
                "symbol": "BTCUSDT",
                "price": "60000.0",
                "qty": "0.01",
                "quoteQty": "600.0",
                "commission": "0.6",
                "commissionAsset": "USDT",
                "time": 1700000000000,
                "isBuyer": True,
                "isMaker": False,
            }
        ]

        def mock_get_balance(use_cache: bool = False) -> Dict[str, float]:
            return {"usdt": 100.0}

        with patch("hermes.trading.reconcile.reconcile_positions_from_binance", return_value=[]), \
             patch("hermes.accounting.ingestion.fetch_paginated_binance_trades", return_value=mock_trades):
            # Run 1
            validate_positions_on_startup(mock_get_balance)

        con1 = get_db_connection(self.db_path)
        try:
            cutovers_run1 = con1.execute("SELECT * FROM accounting_cutovers;").fetchall()
            fills_run1 = con1.execute("SELECT * FROM fills WHERE symbol = 'BTCUSDT';").fetchall()
            self.assertEqual(len(cutovers_run1), 1)
            self.assertEqual(len(fills_run1), 1)
        finally:
            con1.close()

        with patch("hermes.trading.reconcile.reconcile_positions_from_binance", return_value=[]), \
             patch("hermes.accounting.ingestion.fetch_paginated_binance_trades", return_value=mock_trades):
            # Run 2 (simulate daemon restart)
            validate_positions_on_startup(mock_get_balance)

        con2 = get_db_connection(self.db_path)
        try:
            cutovers_run2 = con2.execute("SELECT * FROM accounting_cutovers;").fetchall()
            fills_run2 = con2.execute("SELECT * FROM fills WHERE symbol = 'BTCUSDT';").fetchall()

            # Cutovers and fills must remain strictly deduplicated
            self.assertEqual(len(cutovers_run2), 1)
            self.assertEqual(len(fills_run2), 1)
            self.assertEqual(cutovers_run1[0]["cutover_id"], cutovers_run2[0]["cutover_id"])
            self.assertEqual(cutovers_run1[0]["status"], cutovers_run2[0]["status"])
            self.assertEqual(fills_run1[0]["trade_id"], fills_run2[0]["trade_id"])
        finally:
            con2.close()


    def test_daemon_periodic_sync_invokes_trade_sync_and_collector(self) -> None:
        """Periodic background sync reconciles positions, syncs accounting trades, and runs daily collector."""
        mock_get_balance = MagicMock(return_value={"usdt": 50.0})
        sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])

        with patch("asyncio.sleep", new=sleep_mock), \
             patch("hermes.trading.reconcile.reconcile_positions_from_binance", return_value=[]), \
             patch("hermes.accounting.ingestion.sync_accounting_trades") as mock_sync_trades, \
             patch("hermes.api.transfer.run_daily_profit_collector") as mock_collector:

            try:
                asyncio.run(daemon_periodic_sync(mock_get_balance))
            except asyncio.CancelledError:
                pass

        mock_sync_trades.assert_called_once()
        mock_collector.assert_called_once_with(mock_get_balance)

    def test_execute_sell_triggers_accounting_sync_and_collector(self) -> None:
        """execute_sell triggers sync_accounting_trades and run_daily_profit_collector."""
        from hermes.trading.execution import execute_sell
        state.positions["SOL"] = {
            "entry_price": 100.0,
            "qty": 1.0,
            "time": time.time() - 3600,
            "mode": "NORMAL",
            "sl": 95.0,
            "tp1": 105.0,
            "tp2": 110.0,
        }

        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch("hermes.api.balance.get_balance", return_value={"sol": 1.0, "usdt": 50.0}), \
             patch("hermes.trading.execution._round_qty_to_lot_size", return_value=1.0), \
             patch("hermes.trading.execution.api_call", return_value={"status": "FILLED", "cummulativeQuoteQty": "100.0"}), \
             patch("hermes.accounting.ingestion.sync_accounting_trades") as mock_sync_trades, \
             patch("hermes.api.transfer.run_daily_profit_collector") as mock_collector, \
             patch("hermes.notifications.telegram.telegram_exit_alert"):

            success, msg = execute_sell(pair="SOL", price=100.0, qty=1.0, reason="Take Profit")

        self.assertTrue(success)
        self.assertNotIn("SOL", state.positions)
        mock_sync_trades.assert_called_once_with(symbols=["SOLUSDT"])
        mock_collector.assert_called_once()


if __name__ == "__main__":
    unittest.main()
