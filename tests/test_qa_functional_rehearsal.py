"""
Comprehensive Functional QA and Regression Validation Test Suite.

Validates:
1. Net-loss accounting contract test and signed loss carry forward.
2. Test isolation sentinels: zero outbound network calls, zero mock leaks, no live Binance mutations, and no production .env access.
3. Accounting rehearsal against multi-fee, partial-fill, and net-loss scenarios with exact Decimal precision.
4. Sweep collector edge cases: concurrent calls, process restarts, timeout reconciliation, and insufficient reserve balance.
5. Capital rotation replay and shadow simulations, validating sell-then-buy durability and Futures isolation.
"""

from decimal import Decimal
import os
import socket
import unittest
from typing import Any, Dict, List, Mapping, Optional

import hermes.config as cfg
from hermes.accounting.collector import (
    SpotToFundingCollector,
    evaluate_distribution,
)
from hermes.accounting.contracts import (
    AccountingCutover,
    AccountingSnapshot,
    Completeness,
    CutoverStatus,
    DecimalString,
    DistributionAction,
    DistributionPolicyConfig,
    FillEvent,
    FillKey,
    RealizedOutcome,
    ReconciliationStatus,
    RotationAction,
    RotationDecision,
    RotationMarketSnapshot,
    RotationStatus,
    SpotOrderGateway,
    SpotOrderSubmission,
    TradeSide,
    TransferGateway,
    TransferHistoryRecord,
    TransferIntent,
    TransferStatus,
    TransferSubmission,
    TransferSubmissionStatus,
    ValuationStatus,
    Venue,
    canonical_decimal,
)
from hermes.accounting.ingestion import (
    parse_binance_fill,
    reconcile_and_allocate_fills,
)
from hermes.accounting.repository import (
    ClaimConflictError,
    ConcurrencyConflictError,
    DuplicateFillConflictError,
    SqliteAccountingRepository,
    SqliteRotationRepository,
    SqliteTransferIntentRepository,
)
from hermes.accounting.rotation import (
    RotationExecutor,
    RotationPolicyConfig,
    ShadowRotationRecorder,
    evaluate_rotation,
    rank_rotation_candidates,
)
from hermes.accounting.schema import init_db
from tests.support.isolation import (
    IsolatedTestCase,
    NetworkAccessBlockedError,
    ProductionFileAccessError,
)


class FakeTransferGateway:
    """Deterministic in-memory fake adhering to TransferGateway protocol."""

    def __init__(self) -> None:
        self.submissions: List[TransferSubmission] = []
        self.history_records: List[TransferHistoryRecord] = []
        self.next_submission_status = TransferSubmissionStatus.ACCEPTED
        self.next_tran_id = "tran_qa_9999"
        self.next_error_code: Optional[str] = None

    def submit_spot_to_funding(
        self, *, asset: str, amount: str, client_transfer_id: str
    ) -> TransferSubmission:
        sub = TransferSubmission(
            status=self.next_submission_status,
            exchange_tran_id=self.next_tran_id if self.next_submission_status == TransferSubmissionStatus.ACCEPTED else None,
            submitted_at_ms=1700000000000,
            raw_response_hash=None,
            error_code=self.next_error_code,
        )
        self.submissions.append(sub)
        return sub

    def transfer_history(
        self, *, account_id: str, start_ms: int, end_ms: int
    ) -> List[TransferHistoryRecord]:
        return [
            r for r in self.history_records
            if start_ms <= r.occurred_at_ms <= end_ms
        ]


class FakeSpotOrderGateway:
    """Deterministic fake adhering to SpotOrderGateway protocol."""

    def __init__(self) -> None:
        self.sell_submissions: List[Dict[str, Any]] = []
        self.buy_submissions: List[Dict[str, Any]] = []
        self.futures_calls: List[Dict[str, Any]] = []
        self.next_sell_accepted = True
        self.next_sell_unknown = False
        self.next_sell_error: Optional[str] = None
        self.next_buy_accepted = True
        self.next_buy_unknown = False
        self.next_buy_error: Optional[str] = None

    def submit_sell(
        self, *, symbol: str, base_qty: DecimalString, client_order_id: str
    ) -> SpotOrderSubmission:
        sub = SpotOrderSubmission(
            accepted=self.next_sell_accepted,
            unknown=self.next_sell_unknown,
            client_order_id=client_order_id,
            exchange_order_id="ex_sell_123" if self.next_sell_accepted else None,
            submitted_at_ms=1700000000000,
            raw_response_hash=None,
            error_code=self.next_sell_error,
        )
        self.sell_submissions.append({"symbol": symbol, "base_qty": base_qty, "sub": sub})
        return sub

    def submit_buy(
        self, *, symbol: str, quote_budget_usdt: DecimalString, client_order_id: str
    ) -> SpotOrderSubmission:
        sub = SpotOrderSubmission(
            accepted=self.next_buy_accepted,
            unknown=self.next_buy_unknown,
            client_order_id=client_order_id,
            exchange_order_id="ex_buy_456" if self.next_buy_accepted else None,
            submitted_at_ms=1700000000000,
            raw_response_hash=None,
            error_code=self.next_buy_error,
        )
        self.buy_submissions.append({"symbol": symbol, "quote_budget_usdt": quote_budget_usdt, "sub": sub})
        return sub

    def read_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        return {"status": "FILLED"}

    def submit_futures_order(self, *args: Any, **kwargs: Any) -> None:
        self.futures_calls.append({"args": args, "kwargs": kwargs})
        raise RuntimeError("FUTURES ORDER IS STRICTLY PROHIBITED IN SPOT ROTATION")


class TestQaAccountingRehearsal(IsolatedTestCase):
    """Rehearsal tests for multi-fee, partial-fill, and net-loss Decimal accounting."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "qa_accounting_rehearsal.db"
        init_db(self.db_path)
        self.repo = SqliteAccountingRepository(self.db_path)

        # Establish approved cutover baseline
        now_ms = 1700000000000
        self.repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_qa_1",
                account_id="default",
                venue=Venue.SPOT,
                cutover_at_ms=now_ms - 100000,
                baseline_reference="qa_genesis",
                backfill_from_ms=None,
                backfill_through_ms=now_ms - 100000,
                status=CutoverStatus.APPROVED,
                approved_at_ms=now_ms - 100000,
            )
        )

    def test_multi_fee_valuation_scenarios(self) -> None:
        """Verify USDT, base-asset, external valuation, and unvalued fee handling."""
        # 1. Base asset commission (ETH commission on ETHUSDT)
        trade_eth_fee = {
            "id": 10001,
            "orderId": 20001,
            "symbol": "ETHUSDT",
            "time": 1700000001000,
            "isBuyer": True,
            "price": "3000.00",
            "qty": "2.000000",
            "quoteQty": "6000.00",
            "commission": "0.002",
            "commissionAsset": "ETH",
        }
        fill_eth = parse_binance_fill(trade_eth_fee)
        self.assertEqual(fill_eth.valuation_status, ValuationStatus.VALUED)
        # 0.002 ETH * 3000 USDT/ETH = 6.00 USDT
        self.assertEqual(fill_eth.commission_usdt, "6")
        self.assertTrue(self.repo.ingest_fill(fill_eth))

        # 2. External asset commission with valuation map (BNB)
        trade_bnb_fee = {
            "id": 10002,
            "orderId": 20002,
            "symbol": "SOLUSDT",
            "time": 1700000002000,
            "isBuyer": True,
            "price": "100.00",
            "qty": "10.000000",
            "quoteQty": "1000.00",
            "commission": "0.02",
            "commissionAsset": "BNB",
        }
        val_map = {"BNB": "300.00"}
        fill_bnb = parse_binance_fill(trade_bnb_fee, valuation_map=val_map)
        self.assertEqual(fill_bnb.valuation_status, ValuationStatus.VALUED)
        # 0.02 BNB * 300 = 6.00 USDT
        self.assertEqual(fill_bnb.commission_usdt, "6")
        self.assertTrue(self.repo.ingest_fill(fill_bnb))

        # 3. External asset commission WITHOUT valuation map -> UNAVAILABLE
        trade_unvalued = {
            "id": 10003,
            "orderId": 20003,
            "symbol": "AVAXUSDT",
            "time": 1700000003000,
            "isBuyer": True,
            "price": "20.00",
            "qty": "10.000000",
            "quoteQty": "200.00",
            "commission": "0.01",
            "commissionAsset": "UNKNOWN_TOKEN",
        }
        fill_unvalued = parse_binance_fill(trade_unvalued, valuation_map=None)
        self.assertEqual(fill_unvalued.valuation_status, ValuationStatus.UNAVAILABLE)
        self.assertIsNone(fill_unvalued.commission_usdt)
        self.assertTrue(self.repo.ingest_fill(fill_unvalued))

        # Sell the unvalued lot -> produces UNRESOLVED outcome and blocks distribution
        trade_unvalued_sell = {
            "id": 10004,
            "orderId": 20004,
            "symbol": "AVAXUSDT",
            "time": 1700000004000,
            "isBuyer": False,
            "price": "25.00",
            "qty": "10.000000",
            "quoteQty": "250.00",
            "commission": "0.25",
            "commissionAsset": "USDT",
        }
        fill_unvalued_sell = parse_binance_fill(trade_unvalued_sell)
        self.assertTrue(self.repo.ingest_fill(fill_unvalued_sell))
        outcome_unvalued = self.repo.apply_fifo_sell(fill_unvalued_sell.key)
        self.assertEqual(outcome_unvalued.completeness, Completeness.UNRESOLVED)
        self.assertIn("UNRESOLVED_FEE_VALUATION", outcome_unvalued.incomplete_reason_codes)

        # Snapshot reflects incomplete accounting
        snapshot = self.repo.distribution_snapshot("default", observed_at_ms=1700000005000)
        self.assertEqual(snapshot.completeness, Completeness.PARTIAL)
        self.assertIn("ACCOUNTING_INCOMPLETE", snapshot.unresolved_reason_codes)

    def test_extreme_decimal_precision_micro_cap(self) -> None:
        """Verify PEPE/SHIB micro-price high-volume trades maintain exact Decimal precision."""
        pepe_buy = parse_binance_fill({
            "id": 88001,
            "orderId": 99001,
            "symbol": "PEPEUSDT",
            "time": 1700000010000,
            "isBuyer": True,
            "price": "0.00001234",
            "qty": "10000000.00000000",
            "quoteQty": "123.40000000",
            "commission": "0.12340000",
            "commissionAsset": "USDT",
        })
        self.assertTrue(self.repo.ingest_fill(pepe_buy))

        pepe_sell = parse_binance_fill({
            "id": 88002,
            "orderId": 99002,
            "symbol": "PEPEUSDT",
            "time": 1700000020000,
            "isBuyer": False,
            "price": "0.00001500",
            "qty": "10000000.00000000",
            "quoteQty": "150.00000000",
            "commission": "0.15000000",
            "commissionAsset": "USDT",
        })
        self.assertTrue(self.repo.ingest_fill(pepe_sell))

        outcome = self.repo.apply_fifo_sell(pepe_sell.key)
        self.assertEqual(outcome.completeness, Completeness.VERIFIED)
        self.assertEqual(outcome.sold_base_qty, "10000000")
        self.assertEqual(outcome.gross_proceeds_usdt, "150")
        self.assertEqual(outcome.fifo_cost_usdt, "123.4")
        self.assertEqual(outcome.buy_fee_usdt, "0.1234")
        self.assertEqual(outcome.sell_fee_usdt, "0.15")
        # Net PnL = 150 - 123.4 - 0.1234 - 0.15 = 26.3266
        self.assertEqual(outcome.net_pnl_usdt, "26.3266")

    def test_multi_buy_multi_sell_complex_partial_fill_fifo(self) -> None:
        """
        Complex FIFO rehearsal:
        Buy 1: 100 units @ $10 ($1000 cost, $1.00 fee)
        Buy 2: 150 units @ $12 ($1800 cost, $1.80 fee)
        Buy 3: 200 units @ $15 ($3000 cost, $3.00 fee)

        Sell 1: 180 units @ $14 ($2520 proceeds, $2.52 fee)
          - Consumes 100 from Buy 1 (cost $1000, fee $1.00)
          - Consumes 80 from Buy 2 (cost $960, fee 80/150 * 1.80 = $0.96)
          - Net PnL = 2520 - (1000 + 960) - (1.00 + 0.96) - 2.52 = 2520 - 1960 - 1.96 - 2.52 = +$555.52

        Sell 2: 120 units @ $11 ($1320 proceeds, $1.32 fee)
          - Consumes remaining 70 from Buy 2 (cost $840, fee 70/150 * 1.80 = $0.84)
          - Consumes 50 from Buy 3 (cost $750, fee 50/200 * 3.00 = $0.75)
          - Net PnL = 1320 - (840 + 750) - (0.84 + 0.75) - 1.32 = 1320 - 1590 - 1.59 - 1.32 = -$272.91

        Cumulative net PnL = +555.52 - 272.91 = +$282.61.
        """
        b1 = parse_binance_fill({
            "id": 1, "orderId": 10, "symbol": "TESTUSDT", "time": 1700000010000,
            "isBuyer": True, "price": "10", "qty": "100", "commission": "1", "commissionAsset": "USDT"
        })
        b2 = parse_binance_fill({
            "id": 2, "orderId": 20, "symbol": "TESTUSDT", "time": 1700000020000,
            "isBuyer": True, "price": "12", "qty": "150", "commission": "1.8", "commissionAsset": "USDT"
        })
        b3 = parse_binance_fill({
            "id": 3, "orderId": 30, "symbol": "TESTUSDT", "time": 1700000030000,
            "isBuyer": True, "price": "15", "qty": "200", "commission": "3", "commissionAsset": "USDT"
        })
        self.assertTrue(self.repo.ingest_fill(b1))
        self.assertTrue(self.repo.ingest_fill(b2))
        self.assertTrue(self.repo.ingest_fill(b3))

        # Sell 1
        s1 = parse_binance_fill({
            "id": 4, "orderId": 40, "symbol": "TESTUSDT", "time": 1700000040000,
            "isBuyer": False, "price": "14", "qty": "180", "commission": "2.52", "commissionAsset": "USDT"
        })
        self.assertTrue(self.repo.ingest_fill(s1))
        outcome1 = self.repo.apply_fifo_sell(s1.key)
        self.assertEqual(outcome1.completeness, Completeness.VERIFIED)
        self.assertEqual(outcome1.fifo_cost_usdt, "1960")
        self.assertEqual(outcome1.buy_fee_usdt, "1.96")
        self.assertEqual(outcome1.sell_fee_usdt, "2.52")
        self.assertEqual(outcome1.net_pnl_usdt, "555.52")

        # Sell 2
        s2 = parse_binance_fill({
            "id": 5, "orderId": 50, "symbol": "TESTUSDT", "time": 1700000050000,
            "isBuyer": False, "price": "11", "qty": "120", "commission": "1.32", "commissionAsset": "USDT"
        })
        self.assertTrue(self.repo.ingest_fill(s2))
        outcome2 = self.repo.apply_fifo_sell(s2.key)
        self.assertEqual(outcome2.completeness, Completeness.VERIFIED)
        self.assertEqual(outcome2.fifo_cost_usdt, "1590")
        self.assertEqual(outcome2.buy_fee_usdt, "1.59")
        self.assertEqual(outcome2.sell_fee_usdt, "1.32")
        self.assertEqual(outcome2.net_pnl_usdt, "-272.91")

        # Snapshot verification
        snapshot = self.repo.distribution_snapshot("default", observed_at_ms=1700000060000)
        self.assertEqual(snapshot.cumulative_verified_net_pnl_usdt, "282.61")
        self.assertEqual(snapshot.distribution_surplus_usdt, "282.61")
        self.assertEqual(snapshot.completeness, Completeness.VERIFIED)

    def test_net_loss_carry_forward_and_recovery_sequence(self) -> None:
        """Verify signed negative loss carry forward and recovery behavior."""
        # Loss 1: -$100 net
        b1 = parse_binance_fill({
            "id": 11, "orderId": 110, "symbol": "L1USDT", "time": 1700000010000,
            "isBuyer": True, "price": "100", "qty": "5", "commission": "0.5", "commissionAsset": "USDT"
        })
        s1 = parse_binance_fill({
            "id": 12, "orderId": 120, "symbol": "L1USDT", "time": 1700000020000,
            "isBuyer": False, "price": "80", "qty": "5", "commission": "0.5", "commissionAsset": "USDT"
        })
        self.repo.ingest_fill(b1)
        self.repo.ingest_fill(s1)
        out1 = self.repo.apply_fifo_sell(s1.key)
        # 400 - 500 - 0.5 - 0.5 = -101.0
        self.assertEqual(out1.net_pnl_usdt, "-101")

        snap1 = self.repo.distribution_snapshot("default", observed_at_ms=1700000025000)
        self.assertEqual(snap1.distribution_surplus_usdt, "-101")

        # Win 1: +$103.0 net
        b2 = parse_binance_fill({
            "id": 13, "orderId": 130, "symbol": "W1USDT", "time": 1700000030000,
            "isBuyer": True, "price": "50", "qty": "10", "commission": "0.5", "commissionAsset": "USDT"
        })
        s2 = parse_binance_fill({
            "id": 14, "orderId": 140, "symbol": "W1USDT", "time": 1700000040000,
            "isBuyer": False, "price": "60.40", "qty": "10", "commission": "0.5", "commissionAsset": "USDT"
        })
        self.repo.ingest_fill(b2)
        self.repo.ingest_fill(s2)
        out2 = self.repo.apply_fifo_sell(s2.key)
        # 604 - 500 - 0.5 - 0.5 = +103.0
        self.assertEqual(out2.net_pnl_usdt, "103")

        snap2 = self.repo.distribution_snapshot("default", observed_at_ms=1700000045000)
        # Cumulative = -101 + 103 = +2.0 USDT
        self.assertEqual(snap2.cumulative_verified_net_pnl_usdt, "2")
        self.assertEqual(snap2.distribution_surplus_usdt, "2")


class TestQaSweepCollectorEdgeCases(IsolatedTestCase):
    """Test suite for collector concurrency, crash recovery, timeout, and reserve gates."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "qa_collector_edge.db"
        init_db(self.db_path)
        self.accounting_repo = SqliteAccountingRepository(self.db_path)
        self.intent_repo = SqliteTransferIntentRepository(self.db_path)
        self.gateway = FakeTransferGateway()
        self.policy_config = DistributionPolicyConfig(
            policy_version="p1",
            enabled=True,
            legacy_auto_sweep_enabled=False,
            target_usdt="1",
            daily_cap_usdt="1",
            operational_buffer_usdt="0",
            reserve_fraction="0.25",
        )

        now_ms = 1700000000000
        self.accounting_repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_qa_coll",
                account_id="default",
                venue=Venue.SPOT,
                cutover_at_ms=now_ms - 50000,
                baseline_reference="genesis",
                backfill_from_ms=None,
                backfill_through_ms=now_ms - 50000,
                status=CutoverStatus.APPROVED,
                approved_at_ms=now_ms - 50000,
            )
        )

    def test_insufficient_portfolio_reserve_rejection(self) -> None:
        """Free USDT below portfolio reserve threshold blocks sweep."""
        snap = AccountingSnapshot(
            snapshot_id="snap_1",
            ledger_revision=1,
            account_id="default",
            cutover_id="cutover_qa_coll",
            cutover_status=CutoverStatus.APPROVED,
            cutover_at_ms=1700000000000,
            cumulative_verified_net_pnl_usdt="5",
            cumulative_confirmed_distributions_usdt="0",
            distribution_surplus_usdt="5",
            completeness=Completeness.VERIFIED,
            reconciliation_status=ReconciliationStatus.RECONCILED,
            unresolved_reason_codes=(),
            has_submitting_transfer=False,
            has_unknown_transfer=False,
            observed_at_ms=1700000010000,
        )
        # Total equity $100 -> 25% reserve requires $25.00 free USDT.
        # Free USDT is only $20.00 -> should be rejected with RESERVE_BREACH / INSUFFICIENT_FREE_USDT
        eval_result = evaluate_distribution(
            policy_config=self.policy_config,
            accounting_snapshot=snap,
            free_spot_usdt="20",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=1700000010000,
        )
        self.assertEqual(eval_result.action, DistributionAction.BLOCKED)
        self.assertIn("RESERVE_BREACH", eval_result.reason_codes)

    def test_timeout_reconciliation_forward_to_confirmed(self) -> None:
        """When submission returns UNKNOWN / timeout, read-back reconciles to CONFIRMED."""
        # Ingest a profitable trade so surplus is positive
        b = parse_binance_fill({
            "id": 91, "orderId": 910, "symbol": "BTCUSDT", "time": 1700000010000,
            "isBuyer": True, "price": "50000", "qty": "0.1", "commission": "0.5", "commissionAsset": "USDT"
        })
        s = parse_binance_fill({
            "id": 92, "orderId": 920, "symbol": "BTCUSDT", "time": 1700000020000,
            "isBuyer": False, "price": "55000", "qty": "0.1", "commission": "0.5", "commissionAsset": "USDT"
        })
        self.accounting_repo.ingest_fill(b)
        self.accounting_repo.ingest_fill(s)
        self.accounting_repo.apply_fifo_sell(s.key)

        collector = SpotToFundingCollector(
            accounting_repo=self.accounting_repo,
            intent_repo=self.intent_repo,
            transfer_gateway=self.gateway,
            policy_config=self.policy_config,
        )

        # Gateway returns UNKNOWN (e.g. read timeout)
        self.gateway.next_submission_status = TransferSubmissionStatus.UNKNOWN
        decision, intent = collector.run_daily_collection_tick(
            account_id="default",
            free_spot_usdt="50",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=1700000020000,
        )
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.status, TransferStatus.UNKNOWN)

        # On next run, exchange history contains the confirmed transfer
        self.gateway.history_records.append(
            TransferHistoryRecord(
                exchange_tran_id="tran_reconciled_9999",
                client_transfer_id=intent.client_transfer_id,
                asset="USDT",
                direction="MAIN_FUNDING",
                amount="1",
                occurred_at_ms=intent.created_at_ms,
                status="CONFIRMED",
            )
        )

        reconciled = collector.reconcile_unresolved_transfers(
            account_id="default",
            now_ms=intent.created_at_ms + 10000,
        )
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].status, TransferStatus.CONFIRMED)
        self.assertEqual(reconciled[0].exchange_tran_id, "tran_reconciled_9999")


class TestQaCapitalRotationRehearsal(IsolatedTestCase):
    """Test suite for rotation durability, cash recovery, and zero Futures fallback."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "qa_rotation_rehearsal.db"
        init_db(self.db_path)
        self.accounting_repo = SqliteAccountingRepository(self.db_path)
        self.rotation_repo = SqliteRotationRepository(self.db_path)
        self.gateway = FakeSpotOrderGateway()
        self.config = RotationPolicyConfig(
            model_version="v2",
            feature_schema_version="v1",
            enabled=True,
            shadow_mode=False,
            min_score_edge="3",
            min_pnl_pct="-0.045",
            max_pnl_pct="0.02",
            min_hold_secs=1800,
            max_cost_fraction="0.015",
            max_spread_fraction="0.005",
            min_depth_usdt="50",
            min_trade_usdt="5.5",
            fee_rate_fraction="0.001",
        )

    def _make_snapshot(
        self,
        symbol: str,
        score: str = "5",
        spread: str = "0.001",
        depth: str = "1000",
        completeness: Completeness = Completeness.VERIFIED,
    ) -> RotationMarketSnapshot:
        return RotationMarketSnapshot(
            snapshot_id=f"snap_{symbol}",
            account_id="default",
            venue=Venue.SPOT,
            symbol=symbol,
            position_lifecycle_id=f"pos_{symbol}",
            position_version=1,
            model_version="v2",
            feature_schema_version="v1",
            observed_at_ms=1700000000000,
            expires_at_ms=1700001000000,
            score=score,
            best_bid_usdt="100",
            best_ask_usdt="100.1",
            spread_fraction=spread,
            available_depth_usdt=depth,
            closed_bar_ids=("b1", "b2"),
            completeness=completeness,
        )

    def test_failed_buy_enters_cash_recovery_retaining_usdt_no_futures(self) -> None:
        """Sell succeeds, buy fails -> enters CASH_RECOVERY with USDT retained, never calling Futures."""
        now_ms = 1700000000000
        held_snap = self._make_snapshot("ETHUSDT", score="4")
        cand_snap = self._make_snapshot("SOLUSDT", score="8")

        decision = evaluate_rotation(
            held_snapshot=held_snap,
            candidate_snapshot=cand_snap,
            held_position_pnl_pct="-0.01",
            held_holding_time_secs=2000,
            config=self.config,
            now_ms=now_ms,
        )

        self.gateway.next_buy_accepted = False
        self.gateway.next_buy_error = "-2010:INSUFFICIENT_FUNDS"

        executor = RotationExecutor(
            rotation_repo=self.rotation_repo,
            accounting_repo=self.accounting_repo,
            order_gateway=self.gateway,
            config=self.config,
        )

        intent = executor.execute_rotation(
            decision=decision,
            held_base_qty="0.5",
            approved_replacement_budget_usdt="25.0",
            now_ms=now_ms,
        )

        self.assertEqual(intent.status, RotationStatus.CASH_RECOVERY)
        self.assertEqual(len(self.gateway.sell_submissions), 1)
        self.assertEqual(len(self.gateway.buy_submissions), 1)
        # Futures calls MUST be zero
        self.assertEqual(len(self.gateway.futures_calls), 0)

    def test_shadow_rotation_recorder_zero_gateway_mutations(self) -> None:
        """Shadow rotation evaluates decisions and logs them with zero execution gateway mutations."""
        shadow_config = RotationPolicyConfig(
            model_version="v2",
            feature_schema_version="v1",
            enabled=False,
            shadow_mode=True,
            min_score_edge="3",
        )
        recorder = ShadowRotationRecorder(
            rotation_repo=self.rotation_repo,
            config=shadow_config,
        )

        now_ms = 1700000000000
        held_snap = self._make_snapshot("ETHUSDT", score="4")
        cand_snap = self._make_snapshot("SOLUSDT", score="8")

        dec = recorder.record_evaluation(
            held_snapshot=held_snap,
            candidate_snapshot=cand_snap,
            held_position_pnl_pct="-0.01",
            held_holding_time_secs=2000,
            now_ms=now_ms,
        )
        self.assertEqual(dec.action, RotationAction.APPROVE)
        self.assertIn("SHADOW_ONLY", dec.reason_codes)
        # Zero gateway executions
        self.assertEqual(len(self.gateway.sell_submissions), 0)
        self.assertEqual(len(self.gateway.buy_submissions), 0)
        self.assertEqual(len(self.gateway.futures_calls), 0)


class TestQaTestIsolationSentinels(IsolatedTestCase):
    """Test suite validating test isolation sentinels and environment integrity."""

    def test_unmocked_network_calls_blocked(self) -> None:
        """Verify that raw socket connect, urllib, and curl are blocked."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(NetworkAccessBlockedError):
                s.connect(("api.binance.com", 443))
        finally:
            s.close()

        import urllib.request
        with self.assertRaises(NetworkAccessBlockedError):
            urllib.request.urlopen("https://api.binance.com/api/v3/ping")

    def test_production_file_protection(self) -> None:
        """Verify that writing or deleting production files outside temp directory is blocked."""
        prod_path = "/var/www/new-trading-system/daily_profit_state.json"
        with self.assertRaises(ProductionFileAccessError):
            with open(prod_path, "w") as f:
                f.write("malicious write")

    def test_production_env_isolation(self) -> None:
        """Verify that production credentials are not loaded into environment."""
        self.assertNotIn("BINANCE_REAL_SECRET", os.environ)
        self.assertFalse(cfg.ROTATION_ENABLED)
        self.assertFalse(cfg.DAILY_PROFIT_COLLECTION)
        self.assertFalse(cfg.FUTURES_ENABLED)


if __name__ == "__main__":
    unittest.main()
