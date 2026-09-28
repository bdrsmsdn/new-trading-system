"""Tests for Spot-to-Funding collector pure policy, transfer gateway, and orchestrator."""

from decimal import Decimal
from typing import Optional
import unittest
from unittest.mock import MagicMock

from hermes.accounting.collector import (
    BinanceTransferGateway,
    SpotToFundingCollector,
    evaluate_distribution,
)
from hermes.accounting.contracts import (
    AccountingCutover,
    AccountingSnapshot,
    Completeness,
    CutoverStatus,
    DistributionAction,
    DistributionPolicyConfig,
    ReconciliationStatus,
    TransferGateway,
    TransferHistoryRecord,
    TransferIntent,
    TransferStatus,
    TransferSubmission,
    TransferSubmissionStatus,
    Venue,
)
from hermes.accounting.repository import (
    SqliteAccountingRepository,
    SqliteTransferIntentRepository,
)
from hermes.accounting.schema import init_db
from tests.support.isolation import IsolatedTestCase


class FakeTransferGateway:
    """Deterministic in-memory fake adhering to TransferGateway protocol."""

    def __init__(self) -> None:
        self.submissions: list[TransferSubmission] = []
        self.history_records: list[TransferHistoryRecord] = []
        self.next_submission_status = TransferSubmissionStatus.ACCEPTED
        self.next_tran_id = "tran_9999"
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
    ) -> list[TransferHistoryRecord]:
        return [
            r for r in self.history_records
            if start_ms <= r.occurred_at_ms <= end_ms
        ]


class TestFundingCollector(IsolatedTestCase):
    """Test suite for Spot-to-Funding collector policy and execution."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "collector_test.db"
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

        # Baseline approved cutover
        now_ms = 1700000000000
        self.accounting_repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_c1",
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

    def test_pure_policy_feature_disabled(self) -> None:
        """Disabled policy or enabled legacy auto sweep blocks transfer."""
        snapshot = self.accounting_repo.distribution_snapshot("default", 1700000000000)
        disabled_config = DistributionPolicyConfig(
            policy_version="p1", enabled=False, legacy_auto_sweep_enabled=False
        )
        decision = evaluate_distribution(
            disabled_config, snapshot, free_spot_usdt="100", total_equity_usdt="100",
            open_risk_usdt="0", now_ms=1700000000000
        )
        self.assertEqual(decision.action, DistributionAction.BLOCKED)
        self.assertIn("FEATURE_DISABLED", decision.reason_codes)

    def test_pure_policy_surplus_and_reserve_gates(self) -> None:
        """Verify surplus threshold and portfolio reserve checks."""
        # 1. Snapshot with surplus 0.50 (below 1.00 target) -> NO_ACTION
        snapshot_low = AccountingSnapshot(
            snapshot_id="snap_1", ledger_revision=1, account_id="default",
            cutover_id="c1", cutover_status=CutoverStatus.APPROVED, cutover_at_ms=1700000000000,
            cumulative_verified_net_pnl_usdt="0.50", cumulative_confirmed_distributions_usdt="0",
            distribution_surplus_usdt="0.50", completeness=Completeness.VERIFIED,
            reconciliation_status=ReconciliationStatus.RECONCILED, unresolved_reason_codes=(),
            has_submitting_transfer=False, has_unknown_transfer=False, observed_at_ms=1700000000000
        )
        dec1 = evaluate_distribution(
            self.policy_config, snapshot_low, free_spot_usdt="50", total_equity_usdt="100",
            open_risk_usdt="0", now_ms=1700000000000
        )
        self.assertEqual(dec1.action, DistributionAction.NO_ACTION)
        self.assertIn("SURPLUS_BELOW_TARGET", dec1.reason_codes)

        # 2. Snapshot with surplus 1.50, but free USDT only 0.50 -> BLOCKED (INSUFFICIENT_FREE_USDT)
        snapshot_high = AccountingSnapshot(
            snapshot_id="snap_2", ledger_revision=1, account_id="default",
            cutover_id="c1", cutover_status=CutoverStatus.APPROVED, cutover_at_ms=1700000000000,
            cumulative_verified_net_pnl_usdt="1.50", cumulative_confirmed_distributions_usdt="0",
            distribution_surplus_usdt="1.50", completeness=Completeness.VERIFIED,
            reconciliation_status=ReconciliationStatus.RECONCILED, unresolved_reason_codes=(),
            has_submitting_transfer=False, has_unknown_transfer=False, observed_at_ms=1700000000000
        )
        dec2 = evaluate_distribution(
            self.policy_config, snapshot_high, free_spot_usdt="0.50", total_equity_usdt="100",
            open_risk_usdt="0", now_ms=1700000000000
        )
        self.assertEqual(dec2.action, DistributionAction.BLOCKED)
        self.assertIn("INSUFFICIENT_FREE_USDT", dec2.reason_codes)

        # 3. Free USDT 5.00, but total equity $100 with 25% reserve requires $25 reserve.
        # Post-transfer free USDT would be $4.00 < $25.00 -> BLOCKED (RESERVE_BREACH)
        dec3 = evaluate_distribution(
            self.policy_config, snapshot_high, free_spot_usdt="5.00", total_equity_usdt="100",
            open_risk_usdt="0", now_ms=1700000000000
        )
        self.assertEqual(dec3.action, DistributionAction.BLOCKED)
        self.assertIn("RESERVE_BREACH", dec3.reason_codes)

        # 4. Free USDT $50.00, equity $100 (reserve $25). Post-transfer $49 >= $25 -> PLAN_TRANSFER
        dec4 = evaluate_distribution(
            self.policy_config, snapshot_high, free_spot_usdt="50.00", total_equity_usdt="100",
            open_risk_usdt="0", now_ms=1700000000000
        )
        self.assertEqual(dec4.action, DistributionAction.PLAN_TRANSFER)
        self.assertEqual(dec4.amount_usdt, "1")
        self.assertEqual(dec4.reason_codes, ())

    def test_collector_orchestrator_execution_and_confirmation(self) -> None:
        """Collector claims daily intent, submits to gateway, and transitions to CONFIRMED."""
        collector = SpotToFundingCollector(
            accounting_repo=self.accounting_repo,
            intent_repo=self.intent_repo,
            transfer_gateway=self.gateway,
            policy_config=self.policy_config,
        )

        # Ingest buy and profitable sell to establish surplus >= $1.00
        from hermes.accounting.ingestion import parse_binance_fill
        buy = parse_binance_fill({
            "id": 801, "orderId": 901, "symbol": "BTCUSDT", "time": 1700000010000,
            "isBuyer": True, "price": "50000", "qty": "0.01", "commission": "0.1", "commissionAsset": "USDT"
        })
        sell = parse_binance_fill({
            "id": 802, "orderId": 902, "symbol": "BTCUSDT", "time": 1700000020000,
            "isBuyer": False, "price": "60000", "qty": "0.01", "commission": "0.1", "commissionAsset": "USDT"
        })
        self.accounting_repo.ingest_fill(buy)
        self.accounting_repo.ingest_fill(sell)
        outcome = self.accounting_repo.apply_fifo_sell(sell.key)
        self.assertEqual(outcome.net_pnl_usdt, "99.8")

        # Run tick
        decision, intent = collector.run_daily_collection_tick(
            account_id="default",
            free_spot_usdt="50",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=1700000030000,
        )
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.status, TransferStatus.CONFIRMED)
        self.assertEqual(intent.exchange_tran_id, "tran_9999")

        # Second tick on same day must not create or send a second transfer
        self.gateway.submissions.clear()
        dec2, intent2 = collector.run_daily_collection_tick(
            account_id="default",
            free_spot_usdt="50",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=1700000035000,
        )
        self.assertEqual(len(self.gateway.submissions), 0)

    def test_collector_unknown_response_and_reconciliation(self) -> None:
        """Collector handles UNKNOWN response and reconciles forward without duplicate POST."""
        self.gateway.next_submission_status = TransferSubmissionStatus.UNKNOWN
        self.gateway.next_error_code = "READ_TIMEOUT"

        collector = SpotToFundingCollector(
            accounting_repo=self.accounting_repo,
            intent_repo=self.intent_repo,
            transfer_gateway=self.gateway,
            policy_config=self.policy_config,
        )

        from hermes.accounting.ingestion import parse_binance_fill
        buy = parse_binance_fill({
            "id": 811, "orderId": 911, "symbol": "BTCUSDT", "time": 1700000010000,
            "isBuyer": True, "price": "50000", "qty": "0.01", "commission": "0.1", "commissionAsset": "USDT"
        })
        sell = parse_binance_fill({
            "id": 812, "orderId": 912, "symbol": "BTCUSDT", "time": 1700000020000,
            "isBuyer": False, "price": "60000", "qty": "0.01", "commission": "0.1", "commissionAsset": "USDT"
        })
        self.accounting_repo.ingest_fill(buy)
        self.accounting_repo.ingest_fill(sell)
        self.accounting_repo.apply_fifo_sell(sell.key)

        # Run tick - encounters timeout/unknown
        dec, intent = collector.run_daily_collection_tick(
            account_id="default",
            free_spot_usdt="50",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=1700000030000,
        )
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.status, TransferStatus.UNKNOWN)

        # Add matching record in read-only history
        self.gateway.history_records.append(
            TransferHistoryRecord(
                exchange_tran_id="tran_reconciled_123",
                client_transfer_id=intent.client_transfer_id,
                asset="USDT",
                direction="MAIN_FUNDING",
                amount="1",
                occurred_at_ms=intent.created_at_ms,
                status="SUCCESS",
            )
        )

        # Next tick reconciles intent to CONFIRMED without second POST
        self.gateway.submissions.clear()
        reconciled = collector.reconcile_unresolved_transfers("default", now_ms=intent.created_at_ms + 10000)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].status, TransferStatus.CONFIRMED)
        self.assertEqual(len(self.gateway.submissions), 0)


if __name__ == "__main__":
    unittest.main()
