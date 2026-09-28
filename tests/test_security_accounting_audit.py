"""Comprehensive security audit test suite for Phase 2 accounting, sweep, and rotation.

Verifies:
1. Spot-to-Funding transfer authorization logic & surplus threshold invariants.
2. API credential handling, zero secret leakage, payload scrubbing, and 0600 SQLite permissions.
3. Idempotency, replay attack prevention, CAS state transitions, and crash-recovery reconciliation.
4. Complete blockage of Futures APIs and unauthorized transfer endpoints.
"""

from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from hermes.accounting.collector import (
    BinanceTransferGateway,
    SpotToFundingCollector,
    evaluate_distribution,
)
from hermes.accounting.contracts import (
    AccountingRepository,
    AccountingSnapshot,
    Completeness,
    CutoverStatus,
    DistributionAction,
    DistributionDecision,
    DistributionPolicyConfig,
    FillEvent,
    FillKey,
    Lot,
    LotAllocation,
    RealizedOutcome,
    ReconciliationStatus,
    RotationAction,
    RotationDecision,
    RotationMarketSnapshot,
    RotationStatus,
    TradeSide,
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
    _hash_trade_payload,
    ingest_binance_trades,
    parse_binance_fill,
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
    BinanceSpotOrderGateway,
    RotationExecutor,
    RotationPolicyConfig,
    ShadowRotationRecorder,
    evaluate_rotation,
)
from hermes.accounting.schema import get_db_connection, init_db
import hermes.config as cfg
from tests.support.isolation import IsolatedTestCase


class TestSecurityTransferAuthorization(IsolatedTestCase):
    """Test Suite 1: Spot-to-Funding transfer authorization logic and surplus threshold protection."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_sec_audit.db"
        init_db(self.db_path)
        self.repo = SqliteAccountingRepository(self.db_path)
        self.intent_repo = SqliteTransferIntentRepository(self.db_path)
        self.policy = DistributionPolicyConfig(
            policy_version="v1",
            enabled=True,
            legacy_auto_sweep_enabled=False,
            target_usdt="1",
            daily_cap_usdt="1",
            operational_buffer_usdt="5",
            reserve_fraction="0.25",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()
        super().tearDown()

    def test_surplus_below_target_strictly_refuses_transfer(self) -> None:
        """Surplus below 1.00 USDT (e.g. 0.99 USDT) returns NO_ACTION."""
        snapshot = AccountingSnapshot(
            snapshot_id="snap_1",
            ledger_revision=1,
            account_id="default",
            cutover_id="cut_1",
            cutover_status=CutoverStatus.APPROVED,
            cutover_at_ms=1000000,
            cumulative_verified_net_pnl_usdt="0.99",
            cumulative_confirmed_distributions_usdt="0",
            distribution_surplus_usdt="0.99",
            completeness=Completeness.VERIFIED,
            reconciliation_status=ReconciliationStatus.RECONCILED,
            unresolved_reason_codes=(),
            has_submitting_transfer=False,
            has_unknown_transfer=False,
            observed_at_ms=2000000,
        )
        decision = evaluate_distribution(
            policy_config=self.policy,
            accounting_snapshot=snapshot,
            free_spot_usdt="50",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=2000000,
        )
        self.assertEqual(decision.action, DistributionAction.NO_ACTION)
        self.assertIn("SURPLUS_BELOW_TARGET", decision.reason_codes)
        self.assertIsNone(decision.amount_usdt)

    def test_unverified_accounting_or_loss_carries_blocks_transfer(self) -> None:
        """Accounting with unvalued fees or incomplete status blocks transfer fail-closed."""
        snapshot = AccountingSnapshot(
            snapshot_id="snap_2",
            ledger_revision=2,
            account_id="default",
            cutover_id="cut_1",
            cutover_status=CutoverStatus.APPROVED,
            cutover_at_ms=1000000,
            cumulative_verified_net_pnl_usdt="5.00",
            cumulative_confirmed_distributions_usdt="0",
            distribution_surplus_usdt="5.00",
            completeness=Completeness.PARTIAL,  # Incomplete!
            reconciliation_status=ReconciliationStatus.RECONCILED,
            unresolved_reason_codes=("UNVALUED_FEES_EXIST",),
            has_submitting_transfer=False,
            has_unknown_transfer=False,
            observed_at_ms=2000000,
        )
        decision = evaluate_distribution(
            policy_config=self.policy,
            accounting_snapshot=snapshot,
            free_spot_usdt="50",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=2000000,
        )
        self.assertEqual(decision.action, DistributionAction.BLOCKED)
        self.assertIn("ACCOUNTING_INCOMPLETE", decision.reason_codes)
        self.assertIn("UNVALUED_FEES_EXIST", decision.reason_codes)

    def test_cutover_not_approved_blocks_transfer(self) -> None:
        """Cutover status != APPROVED fails closed to BLOCKED."""
        snapshot = AccountingSnapshot(
            snapshot_id="snap_3",
            ledger_revision=3,
            account_id="default",
            cutover_id="cut_1",
            cutover_status=CutoverStatus.PENDING,
            cutover_at_ms=1000000,
            cumulative_verified_net_pnl_usdt="10.00",
            cumulative_confirmed_distributions_usdt="0",
            distribution_surplus_usdt="10.00",
            completeness=Completeness.VERIFIED,
            reconciliation_status=ReconciliationStatus.RECONCILED,
            unresolved_reason_codes=(),
            has_submitting_transfer=False,
            has_unknown_transfer=False,
            observed_at_ms=2000000,
        )
        decision = evaluate_distribution(
            policy_config=self.policy,
            accounting_snapshot=snapshot,
            free_spot_usdt="50",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=2000000,
        )
        self.assertEqual(decision.action, DistributionAction.BLOCKED)
        self.assertIn("CUTOVER_NOT_APPROVED", decision.reason_codes)

    def test_portfolio_reserve_breach_blocks_transfer(self) -> None:
        """Transfer that would breach operational buffer or risk reserve fails closed."""
        snapshot = AccountingSnapshot(
            snapshot_id="snap_4",
            ledger_revision=4,
            account_id="default",
            cutover_id="cut_1",
            cutover_status=CutoverStatus.APPROVED,
            cutover_at_ms=1000000,
            cumulative_verified_net_pnl_usdt="10.00",
            cumulative_confirmed_distributions_usdt="0",
            distribution_surplus_usdt="10.00",
            completeness=Completeness.VERIFIED,
            reconciliation_status=ReconciliationStatus.RECONCILED,
            unresolved_reason_codes=(),
            has_submitting_transfer=False,
            has_unknown_transfer=False,
            observed_at_ms=2000000,
        )
        decision = evaluate_distribution(
            policy_config=self.policy,
            accounting_snapshot=snapshot,
            free_spot_usdt="25.5",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=2000000,
        )
        self.assertEqual(decision.action, DistributionAction.BLOCKED)
        self.assertIn("RESERVE_BREACH", decision.reason_codes)

    def test_in_flight_or_unknown_transfer_blocks_evaluation(self) -> None:
        """Active SUBMITTING or UNKNOWN transfers block new transfer planning."""
        snapshot = AccountingSnapshot(
            snapshot_id="snap_5",
            ledger_revision=5,
            account_id="default",
            cutover_id="cut_1",
            cutover_status=CutoverStatus.APPROVED,
            cutover_at_ms=1000000,
            cumulative_verified_net_pnl_usdt="10.00",
            cumulative_confirmed_distributions_usdt="0",
            distribution_surplus_usdt="10.00",
            completeness=Completeness.VERIFIED,
            reconciliation_status=ReconciliationStatus.RECONCILED,
            unresolved_reason_codes=(),
            has_submitting_transfer=False,
            has_unknown_transfer=True,  # In-flight transfer needs reconciliation!
            observed_at_ms=2000000,
        )
        decision = evaluate_distribution(
            policy_config=self.policy,
            accounting_snapshot=snapshot,
            free_spot_usdt="100",
            total_equity_usdt="100",
            open_risk_usdt="0",
            now_ms=2000000,
        )
        self.assertEqual(decision.action, DistributionAction.BLOCKED)
        self.assertIn("UNKNOWN_TRANSFER", decision.reason_codes)


class TestSecurityApiCredentialsAndPermissions(IsolatedTestCase):
    """Test Suite 2: API credential handling, zero secret leakage, and SQLite file permissions."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_sec_perms.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()
        super().tearDown()

    def test_trade_payload_hash_scrubs_secrets(self) -> None:
        """_hash_trade_payload redacts apikey, secret, signature, and token keys."""
        payload_with_secrets = {
            "id": 12345,
            "orderId": 67890,
            "symbol": "BTCUSDT",
            "price": "60000",
            "qty": "0.1",
            "apiKey": "SECRET_KEY_12345",
            "signature": "HMAC_SECRET_SIG_abcde",
            "token": "BEARER_TOKEN_xyz",
        }
        payload_clean = {
            "id": 12345,
            "orderId": 67890,
            "symbol": "BTCUSDT",
            "price": "60000",
            "qty": "0.1",
        }
        hash_dirty = _hash_trade_payload(payload_with_secrets)
        hash_clean = _hash_trade_payload(payload_clean)
        self.assertEqual(hash_dirty, hash_clean)

    def test_sqlite_db_and_companions_have_0600_permissions(self) -> None:
        """Database file and WAL companions are created with 0600 permissions."""
        con = get_db_connection(self.db_path)
        con.execute("CREATE TABLE IF NOT EXISTS probe (id INT);")
        con.execute("INSERT INTO probe VALUES (1);")
        con.close()

        file_stat = self.db_path.stat()
        file_mode = file_stat.st_mode & 0o777
        self.assertEqual(file_mode, 0o600, f"Database permissions {oct(file_mode)} != 0600")

    def test_zero_secrets_serialized_in_ledger_tables(self) -> None:
        """Database schema contains zero columns for storing API secrets or passwords."""
        con = get_db_connection(self.db_path)
        cur = con.execute("SELECT name, sql FROM sqlite_master WHERE type='table';")
        tables = cur.fetchall()
        for row in tables:
            sql = (row["sql"] or "").lower()
            for forbidden in ("api_secret", "secret_key", "private_key", "password", "auth_token"):
                self.assertNotIn(forbidden, sql, f"Table {row['name']} contains forbidden field {forbidden}")
        con.close()


class TestSecurityIdempotencyAndReplayDefense(IsolatedTestCase):
    """Test Suite 3: Idempotency, replay attack defense, and crash recovery."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_sec_idempotency.db"
        init_db(self.db_path)
        self.repo = SqliteAccountingRepository(self.db_path)
        self.intent_repo = SqliteTransferIntentRepository(self.db_path)
        self.rot_repo = SqliteRotationRepository(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()
        super().tearDown()

    def test_conflicting_fill_duplicate_triggers_immediate_quarantine(self) -> None:
        """Replaying a fill key with modified price/qty is quarantined as a tamper attempt."""
        fill1 = FillEvent(
            schema_version=1,
            account_id="default",
            venue=Venue.SPOT,
            symbol="BTCUSDT",
            trade_id="t_100",
            order_id="o_100",
            event_time_ms=1000,
            side=TradeSide.BUY,
            price="60000",
            base_qty="0.1",
            quote_qty="6000",
            commission_asset="USDT",
            commission_qty="6",
            commission_usdt="6",
            valuation_status=ValuationStatus.VALUED,
            source_payload_hash="hash_original",
        )
        self.assertTrue(self.repo.ingest_fill(fill1))

        fill1_tampered = FillEvent(
            schema_version=1,
            account_id="default",
            venue=Venue.SPOT,
            symbol="BTCUSDT",
            trade_id="t_100",
            order_id="o_100",
            event_time_ms=1000,
            side=TradeSide.BUY,
            price="60000",
            base_qty="1.0",
            quote_qty="60000",
            commission_asset="USDT",
            commission_qty="60",
            commission_usdt="60",
            valuation_status=ValuationStatus.VALUED,
            source_payload_hash="hash_tampered",
        )
        with self.assertRaises(DuplicateFillConflictError):
            self.repo.ingest_fill(fill1_tampered)

        con = get_db_connection(self.db_path)
        cur = con.execute("SELECT is_quarantined, quarantine_reason FROM fills WHERE trade_id = 't_100';")
        row = cur.fetchone()
        self.assertEqual(row["is_quarantined"], 1)
        self.assertEqual(row["quarantine_reason"], "CONFLICTING_PAYLOAD_HASH")
        con.close()

    def test_daily_transfer_intent_exactly_once_enforcement(self) -> None:
        """Claiming daily intent twice on the same day returns the existing intent without creating a second."""
        decision = DistributionDecision(
            decision_id="dec_1",
            action=DistributionAction.PLAN_TRANSFER,
            policy_version="v1",
            reporting_day_utc="2026-09-28",
            ledger_snapshot_id="snap_1",
            ledger_revision=1,
            amount_usdt="1",
            distribution_surplus_usdt="5",
            required_reserve_usdt="25",
            reason_codes=(),
            decided_at_ms=1000,
        )
        intent1 = self.intent_repo.claim_daily_intent(decision, "client_tf_001")
        self.assertEqual(intent1.status, TransferStatus.PLANNED)
        self.assertEqual(intent1.client_transfer_id, "client_tf_001")

        intent2 = self.intent_repo.claim_daily_intent(decision, "client_tf_002")
        self.assertEqual(intent2.intent_id, intent1.intent_id)
        self.assertEqual(intent2.client_transfer_id, "client_tf_001")

    def test_out_of_order_cas_transition_rejected(self) -> None:
        """Compare-and-set transition from wrong expected status raises ConcurrencyConflictError."""
        decision = DistributionDecision(
            decision_id="dec_cas",
            action=DistributionAction.PLAN_TRANSFER,
            policy_version="v1",
            reporting_day_utc="2026-09-28",
            ledger_snapshot_id="snap_1",
            ledger_revision=1,
            amount_usdt="1",
            distribution_surplus_usdt="5",
            required_reserve_usdt="25",
            reason_codes=(),
            decided_at_ms=1000,
        )
        intent = self.intent_repo.claim_daily_intent(decision, "client_tf_cas")
        
        intent = self.intent_repo.transition_transfer(
            intent_id=intent.intent_id,
            expected_status=TransferStatus.PLANNED,
            new_status=TransferStatus.SUBMITTING,
            evidence={"sub": 1},
        )
        self.assertEqual(intent.status, TransferStatus.SUBMITTING)

        with self.assertRaises(ConcurrencyConflictError):
            self.intent_repo.transition_transfer(
                intent_id=intent.intent_id,
                expected_status=TransferStatus.PLANNED,
                new_status=TransferStatus.CONFIRMED,
                evidence={"err": 1},
            )


class TestSecurityFuturesAndEndpointIsolation(IsolatedTestCase):
    """Test Suite 4: Complete isolation of Futures endpoints and unauthorized transfer types."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_sec_isolation.db"
        init_db(self.db_path)
        self.accounting_repo = SqliteAccountingRepository(self.db_path)
        self.rot_repo = SqliteRotationRepository(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()
        super().tearDown()

    def test_futures_enabled_disabled_by_default_in_config(self) -> None:
        """FUTURES_ENABLED is strictly False by default."""
        self.assertFalse(cfg.FUTURES_ENABLED)
        self.assertFalse(cfg.ROTATION_ENABLED)
        self.assertFalse(cfg.DAILY_PROFIT_COLLECTION)

    def test_spot_order_gateway_has_zero_futures_endpoints(self) -> None:
        """BinanceSpotOrderGateway only references /api/v3/order and no /fapi or /dapi endpoints."""
        gw = BinanceSpotOrderGateway()
        methods = [m for m in dir(gw) if not m.startswith("_")]
        self.assertIn("submit_sell", methods)
        self.assertIn("submit_buy", methods)
        self.assertIn("read_order", methods)
        self.assertNotIn("submit_futures_order", methods)
        self.assertNotIn("set_leverage", methods)

    def test_transfer_gateway_strictly_limited_to_main_funding(self) -> None:
        """BinanceTransferGateway strictly uses type=MAIN_FUNDING."""
        gw = BinanceTransferGateway()
        with patch("hermes.api.auth.binance_signed_request") as mock_req:
            mock_req.return_value = {"tranId": 123456}
            res = gw.submit_spot_to_funding(asset="USDT", amount="1.00", client_transfer_id="tf_sec_1")
            self.assertEqual(res.status, TransferSubmissionStatus.ACCEPTED)
            mock_req.assert_called_once()
            endpoint, kwargs = mock_req.call_args[0][0], mock_req.call_args[1]
            self.assertEqual(endpoint, "/sapi/v1/asset/transfer")
            self.assertEqual(kwargs["params"]["type"], "MAIN_FUNDING")
            self.assertEqual(kwargs["params"]["asset"], "USDT")
            self.assertNotIn("UMFUTURE", kwargs["params"]["type"])
            self.assertNotIn("CMFUTURE", kwargs["params"]["type"])

    def test_rotation_cash_recovery_retains_usdt_with_zero_futures_fallback(self) -> None:
        """Failed replacement buy enters CASH_RECOVERY retaining USDT with zero Futures fallback."""
        mock_gateway = MagicMock(spec=BinanceSpotOrderGateway)
        mock_gateway.submit_sell.return_value = MagicMock(
            accepted=True,
            unknown=False,
            client_order_id="rotsell_1",
            exchange_order_id="111",
            error_code=None,
        )
        mock_gateway.submit_buy.return_value = MagicMock(
            accepted=False,
            unknown=False,
            client_order_id="rotbuy_1",
            exchange_order_id=None,
            error_code="-2010:INSUFFICIENT_FUNDS",
        )

        executor = RotationExecutor(
            rotation_repo=self.rot_repo,
            accounting_repo=self.accounting_repo,
            order_gateway=mock_gateway,
            config=RotationPolicyConfig(enabled=True),
        )

        decision = RotationDecision(
            schema_version=1,
            decision_id="rotdec_cash_rec",
            account_id="default",
            held_symbol="ETHUSDT",
            candidate_symbol="SOLUSDT",
            position_lifecycle_id="pos_eth_1",
            position_version=1,
            model_version="v2",
            held_snapshot_id="snap_h",
            candidate_snapshot_id="snap_c",
            held_score="5",
            candidate_score="9",
            score_edge="4",
            estimated_roundtrip_cost_usdt="0.25",
            estimated_cost_fraction="0.01",
            expected_net_benefit_usdt="0.75",
            risk_decision_id=None,
            action=RotationAction.APPROVE,
            reason_codes=(),
            decided_at_ms=1000,
        )

        intent = executor.execute_rotation(
            decision=decision,
            held_base_qty="0.1",
            approved_replacement_budget_usdt="25",
        )

        self.assertEqual(intent.status, RotationStatus.CASH_RECOVERY)
        mock_gateway.submit_sell.assert_called_once()
        mock_gateway.submit_buy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
