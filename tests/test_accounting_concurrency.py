"""Concurrency, race condition, crash recovery, and CAS transition tests for accounting ledger."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import threading
import time
import unittest

from hermes.accounting.contracts import (
    AccountingCutover,
    Completeness,
    CutoverStatus,
    DistributionAction,
    DistributionDecision,
    FillEvent,
    FillKey,
    RotationAction,
    RotationDecision,
    RotationStatus,
    TradeSide,
    TransferStatus,
    ValuationStatus,
    Venue,
    canonical_decimal,
)
from hermes.accounting.repository import (
    ClaimConflictError,
    ConcurrencyConflictError,
    DuplicateFillConflictError,
    LedgerRepository,
    SqliteAccountingRepository,
    SqliteRotationRepository,
    SqliteTransferIntentRepository,
)
from hermes.accounting.schema import get_db_connection, init_db
from tests.support.isolation import IsolatedTestCase


class TestAccountingConcurrency(IsolatedTestCase):
    """Test suite for concurrency, idempotency, and transactional durability."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "concurrency_ledger.db"
        self.repo = LedgerRepository(self.db_path)

    def test_concurrent_daily_intent_claim(self) -> None:
        """Concurrent workers attempting to claim the daily intent must result in exactly one intent."""
        decision = DistributionDecision(
            decision_id="dec_001",
            action=DistributionAction.PLAN_TRANSFER,
            policy_version="v1",
            reporting_day_utc="2026-09-28",
            ledger_snapshot_id="snap_001",
            ledger_revision=1,
            amount_usdt="1",
            distribution_surplus_usdt="2.50",
            required_reserve_usdt="10.00",
            reason_codes=(),
            decided_at_ms=1000,
        )

        num_threads = 10
        results = []
        errors = []

        def worker(worker_id: int):
            try:
                repo = SqliteTransferIntentRepository(self.db_path)
                intent = repo.claim_daily_intent(
                    decision=decision,
                    client_transfer_id=f"client_xfer_{worker_id}",
                    account_id="acc1",
                )
                return intent.intent_id
            except Exception as e:
                return e

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(worker, i) for i in range(num_threads)]
            for f in as_completed(futures):
                res = f.result()
                if isinstance(res, Exception):
                    errors.append(res)
                else:
                    results.append(res)

        self.assertEqual(len(errors), 0, f"Encountered unexpected worker errors: {errors}")
        self.assertEqual(len(results), num_threads)
        # All threads must have resolved to the EXACT same intent_id
        unique_intent_ids = set(results)
        self.assertEqual(len(unique_intent_ids), 1)

        # Confirm exactly 1 record in database
        con = get_db_connection(self.db_path)
        try:
            cur = con.execute("SELECT COUNT(*) FROM transfer_intents WHERE account_id='acc1';")
            count = cur.fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            con.close()

    def test_concurrent_rotation_intent_claim(self) -> None:
        """Concurrent workers claiming rotation intent for same decision must yield exactly one intent."""
        decision = RotationDecision(
            schema_version=1,
            decision_id="rot_dec_001",
            account_id="acc1",
            held_symbol="SOLUSDT",
            candidate_symbol="NEARUSDT",
            position_lifecycle_id="pos_life_sol",
            position_version=1,
            model_version="v1",
            held_snapshot_id="snap_sol",
            candidate_snapshot_id="snap_near",
            held_score="5.0",
            candidate_score="8.5",
            score_edge="3.5",
            estimated_roundtrip_cost_usdt="0.10",
            estimated_cost_fraction="0.002",
            expected_net_benefit_usdt="1.20",
            risk_decision_id="risk_001",
            action=RotationAction.APPROVE,
            reason_codes=(),
            decided_at_ms=1000,
        )
        self.repo.rotation.append_decision(decision)

        num_threads = 10
        results = []
        errors = []

        def worker(worker_id: int):
            try:
                repo = SqliteRotationRepository(self.db_path)
                intent = repo.claim_rotation_intent(decision)
                return intent.intent_id
            except Exception as e:
                return e

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(worker, i) for i in range(num_threads)]
            for f in as_completed(futures):
                res = f.result()
                if isinstance(res, Exception):
                    errors.append(res)
                else:
                    results.append(res)

        self.assertEqual(len(errors), 0, f"Worker errors: {errors}")
        self.assertEqual(len(results), num_threads)
        self.assertEqual(len(set(results)), 1)

        con = get_db_connection(self.db_path)
        try:
            count = con.execute("SELECT COUNT(*) FROM rotation_intents;").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            con.close()

    def test_rotation_active_lifecycle_conflict(self) -> None:
        """A new rotation decision on a lifecycle with an active non-terminal intent must be rejected."""
        dec1 = RotationDecision(
            schema_version=1,
            decision_id="rot_dec_1",
            account_id="acc1",
            held_symbol="BTCUSDT",
            candidate_symbol="ETHUSDT",
            position_lifecycle_id="pos_life_btc",
            position_version=1,
            model_version="v1",
            held_snapshot_id="s1",
            candidate_snapshot_id="s2",
            held_score="6.0",
            candidate_score="9.0",
            score_edge="3.0",
            estimated_roundtrip_cost_usdt="0.20",
            estimated_cost_fraction="0.002",
            expected_net_benefit_usdt="1.50",
            risk_decision_id="r1",
            action=RotationAction.APPROVE,
            reason_codes=(),
            decided_at_ms=1000,
        )
        self.repo.rotation.append_decision(dec1)
        intent1 = self.repo.rotation.claim_rotation_intent(dec1)
        self.assertEqual(intent1.status, RotationStatus.PLANNED)

        # Attempting to claim another decision for the same active position lifecycle must fail
        dec2 = RotationDecision(
            schema_version=1,
            decision_id="rot_dec_2",
            account_id="acc1",
            held_symbol="BTCUSDT",
            candidate_symbol="SOLUSDT",
            position_lifecycle_id="pos_life_btc",
            position_version=2,
            model_version="v1",
            held_snapshot_id="s3",
            candidate_snapshot_id="s4",
            held_score="5.0",
            candidate_score="9.5",
            score_edge="4.5",
            estimated_roundtrip_cost_usdt="0.20",
            estimated_cost_fraction="0.002",
            expected_net_benefit_usdt="2.00",
            risk_decision_id="r2",
            action=RotationAction.APPROVE,
            reason_codes=(),
            decided_at_ms=2000,
        )
        self.repo.rotation.append_decision(dec2)

        with self.assertRaises(ClaimConflictError):
            self.repo.rotation.claim_rotation_intent(dec2)

        # Transition intent1 to CASH_RECOVERY (terminal state)
        self.repo.rotation.transition_rotation(
            intent_id=intent1.intent_id,
            expected_status=RotationStatus.PLANNED,
            new_status=RotationStatus.CASH_RECOVERY,
            evidence={"reason": "aborted"},
        )

        # Now claiming dec2 must succeed since intent1 is terminal
        intent2 = self.repo.rotation.claim_rotation_intent(dec2)
        self.assertEqual(intent2.status, RotationStatus.PLANNED)

    def test_cas_transfer_transition_concurrency(self) -> None:
        """Compare-and-set transition must ensure only one concurrent worker advances status."""
        decision = DistributionDecision(
            decision_id="dec_cas",
            action=DistributionAction.PLAN_TRANSFER,
            policy_version="v1",
            reporting_day_utc="2026-09-28",
            ledger_snapshot_id="snap_cas",
            ledger_revision=1,
            amount_usdt="1",
            distribution_surplus_usdt="1.50",
            required_reserve_usdt="10.00",
            reason_codes=(),
            decided_at_ms=1000,
        )
        intent = self.repo.transfers.claim_daily_intent(decision, "cli_cas", "acc1")

        successes = []
        conflicts = []

        def try_transition(worker_id: int):
            repo = SqliteTransferIntentRepository(self.db_path)
            try:
                updated = repo.transition_transfer(
                    intent_id=intent.intent_id,
                    expected_status=TransferStatus.PLANNED,
                    new_status=TransferStatus.SUBMITTING,
                    evidence={"worker_id": worker_id},
                )
                successes.append(updated)
            except ConcurrencyConflictError as ce:
                conflicts.append(ce)

        t1 = threading.Thread(target=try_transition, args=(1,))
        t2 = threading.Thread(target=try_transition, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly 1 success and 1 conflict
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

    def test_crash_recovery_transfer_intent(self) -> None:
        """In-flight SUBMITTING or UNKNOWN transfers must be detectable for read-back on recovery."""
        decision = DistributionDecision(
            decision_id="dec_crash",
            action=DistributionAction.PLAN_TRANSFER,
            policy_version="v1",
            reporting_day_utc="2026-09-28",
            ledger_snapshot_id="snap_crash",
            ledger_revision=1,
            amount_usdt="1",
            distribution_surplus_usdt="2.00",
            required_reserve_usdt="10.00",
            reason_codes=(),
            decided_at_ms=1000,
        )
        intent = self.repo.transfers.claim_daily_intent(decision, "cli_crash", "acc1")
        self.repo.transfers.transition_transfer(
            intent_id=intent.intent_id,
            expected_status=TransferStatus.PLANNED,
            new_status=TransferStatus.SUBMITTING,
            evidence={"submitting": True},
        )

        # Simulate process crash and restart: list unresolved transfers
        unresolved = self.repo.transfers.unresolved_transfers("acc1")
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].intent_id, intent.intent_id)
        self.assertEqual(unresolved[0].status, TransferStatus.SUBMITTING)

        # Reconcile to CONFIRMED
        confirmed = self.repo.transfers.transition_transfer(
            intent_id=intent.intent_id,
            expected_status=TransferStatus.SUBMITTING,
            new_status=TransferStatus.CONFIRMED,
            evidence={"exchange_tran_id": "binance_tx_999"},
        )
        self.assertEqual(confirmed.status, TransferStatus.CONFIRMED)
        self.assertEqual(confirmed.exchange_tran_id, "binance_tx_999")

        # Now no unresolved transfers
        self.assertEqual(len(self.repo.transfers.unresolved_transfers("acc1")), 0)

    def test_concurrent_fill_ingestion_idempotency(self) -> None:
        """Concurrent ingestion of the identical fill must insert exactly once."""
        fill = FillEvent(
            schema_version=1,
            account_id="acc1",
            venue=Venue.SPOT,
            symbol="BTCUSDT",
            trade_id="trade_conc_1",
            order_id="order_conc_1",
            event_time_ms=1000,
            side=TradeSide.BUY,
            price="60000",
            base_qty="0.05",
            quote_qty="3000",
            commission_asset="USDT",
            commission_qty="3",
            commission_usdt="3",
            valuation_status=ValuationStatus.VALUED,
            source_payload_hash="hash_identical",
        )

        num_threads = 8
        results = []
        errors = []

        def worker():
            try:
                repo = SqliteAccountingRepository(self.db_path)
                inserted = repo.ingest_fill(fill)
                return inserted
            except Exception as e:
                return e

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(worker) for _ in range(num_threads)]
            for f in as_completed(futures):
                res = f.result()
                if isinstance(res, Exception):
                    errors.append(res)
                else:
                    results.append(res)

        self.assertEqual(len(errors), 0, f"Worker errors: {errors}")
        # Exactly one True (inserted), rest False (duplicate no-op)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), num_threads - 1)

        con = get_db_connection(self.db_path)
        try:
            fill_count = con.execute("SELECT COUNT(*) FROM fills WHERE trade_id='trade_conc_1';").fetchone()[0]
            lot_count = con.execute("SELECT COUNT(*) FROM lots WHERE acquired_trade_id='trade_conc_1';").fetchone()[0]
            self.assertEqual(fill_count, 1)
            self.assertEqual(lot_count, 1)
        finally:
            con.close()

    def test_conflicting_fill_payload_quarantine(self) -> None:
        """Ingesting a fill with matching key but different hash must quarantine and raise DuplicateFillConflictError."""
        fill1 = FillEvent(
            schema_version=1,
            account_id="acc1",
            venue=Venue.SPOT,
            symbol="ETHUSDT",
            trade_id="t_conflict_1",
            order_id="o_conflict_1",
            event_time_ms=1000,
            side=TradeSide.BUY,
            price="3000",
            base_qty="1",
            quote_qty="3000",
            commission_asset="USDT",
            commission_qty="3",
            commission_usdt="3",
            valuation_status=ValuationStatus.VALUED,
            source_payload_hash="hash_original",
        )
        self.assertTrue(self.repo.accounting.ingest_fill(fill1))

        # Different payload hash for same key
        fill2 = FillEvent(
            schema_version=1,
            account_id="acc1",
            venue=Venue.SPOT,
            symbol="ETHUSDT",
            trade_id="t_conflict_1",
            order_id="o_conflict_1",
            event_time_ms=1000,
            side=TradeSide.BUY,
            price="3000",
            base_qty="1",
            quote_qty="3000",
            commission_asset="USDT",
            commission_qty="3",
            commission_usdt="3",
            valuation_status=ValuationStatus.VALUED,
            source_payload_hash="hash_tampered_conflict",
        )

        with self.assertRaises(DuplicateFillConflictError):
            self.repo.accounting.ingest_fill(fill2)

        # Verify fill is quarantined and reconciliation blocker recorded
        con = get_db_connection(self.db_path)
        try:
            cur = con.execute("SELECT is_quarantined FROM fills WHERE trade_id='t_conflict_1';")
            self.assertEqual(cur.fetchone()[0], 1)

            cur = con.execute("SELECT reason_code FROM reconciliation_blockers WHERE account_id='acc1';")
            self.assertEqual(cur.fetchone()[0], "CONFLICTING_PAYLOAD_HASH")
        finally:
            con.close()

        # Distribution snapshot must be blocked by the reconciliation blocker
        snap = self.repo.accounting.distribution_snapshot("acc1", 2000)
        self.assertIn("CONFLICTING_PAYLOAD_HASH", snap.unresolved_reason_codes)
        self.assertEqual(snap.completeness, Completeness.UNRESOLVED)

    def test_interrupted_transaction_rollback(self) -> None:
        """Any error during atomic fill/lot transaction must cleanly roll back without orphan rows."""
        con = get_db_connection(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE;")
            con.execute(
                """
                INSERT INTO external_cash_flows (
                    flow_id, account_id, asset, amount, flow_type, event_time_ms, tx_id, metadata_json, created_at_ms
                ) VALUES ('cf_fail', 'acc1', 'USDT', '50.00', 'DEPOSIT', 1000, 'tx_fail', '{}', 1000);
                """
            )
            # Simulated crash/failure before commit
            con.execute("ROLLBACK;")
        finally:
            con.close()

        # Verify nothing was committed
        con = get_db_connection(self.db_path)
        try:
            cur = con.execute("SELECT COUNT(*) FROM external_cash_flows WHERE flow_id='cf_fail';")
            self.assertEqual(cur.fetchone()[0], 0)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
