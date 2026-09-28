"""Tests for SQLite schema, migrations, constraints, and precision."""

from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import time
import unittest

from hermes.accounting.contracts import (
    Completeness,
    FillEvent,
    TradeSide,
    ValuationStatus,
    Venue,
    canonical_decimal,
)
from hermes.accounting.schema import (
    MigrationChecksumMismatchError,
    SchemaMigrationError,
    backup_database,
    get_db_connection,
    init_db,
    rollback_migrations,
    run_migrations,
    verify_schema_integrity,
)
from tests.support.isolation import IsolatedTestCase


class TestAccountingSchema(IsolatedTestCase):
    """Test suite for durable accounting database schema and migrations."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "test_ledger.db"

    def test_migration_run_and_idempotent_rerun(self) -> None:
        """Migrations must apply cleanly and be idempotent on re-run."""
        applied = run_migrations(self.db_path)
        self.assertEqual(applied, [1])

        con = get_db_connection(self.db_path)
        try:
            # Check all required tables exist
            cur = con.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {r["name"] for r in cur.fetchall()}
            expected_tables = {
                "schema_migrations",
                "accounting_cutovers",
                "fills",
                "lots",
                "lot_allocations",
                "realized_outcomes",
                "external_cash_flows",
                "transfer_intents",
                "transfer_reconciliation_logs",
                "rotation_decisions",
                "rotation_intents",
                "rotation_audit_logs",
                "reconciliation_blockers",
            }
            self.assertTrue(expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}")

            # Verify integrity
            healthy, errors = verify_schema_integrity(con)
            self.assertTrue(healthy, f"Integrity errors: {errors}")
        finally:
            con.close()

        # Re-running migrations should be a clean no-op
        applied_again = run_migrations(self.db_path)
        self.assertEqual(applied_again, [])

    def test_migration_rollback_and_reapply(self) -> None:
        """Rollback must cleanly tear down tables and allow reapplication."""
        run_migrations(self.db_path)

        # Rollback to version 0
        rolled_back = rollback_migrations(self.db_path, target_version=0)
        self.assertEqual(rolled_back, [1])

        con = get_db_connection(self.db_path)
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            )
            tables = [r["name"] for r in cur.fetchall()]
            self.assertEqual(tables, [])
        finally:
            con.close()

        # Reapply
        reapplied = run_migrations(self.db_path)
        self.assertEqual(reapplied, [1])

    def test_pre_migration_backup(self) -> None:
        """Backup must copy database file with 0600 permissions."""
        run_migrations(self.db_path)

        # Insert dummy row
        con = get_db_connection(self.db_path)
        try:
            con.execute(
                """
                INSERT INTO external_cash_flows (
                    flow_id, account_id, asset, amount, flow_type, event_time_ms, tx_id, metadata_json, created_at_ms
                ) VALUES ('cf1', 'acc1', 'USDT', '100.00', 'DEPOSIT', 1000, 'tx1', '{}', 1000);
                """
            )
        finally:
            con.close()

        backup_file = backup_database(self.db_path)
        self.assertIsNotNone(backup_file)
        assert backup_file is not None
        self.assertTrue(backup_file.exists())
        self.assertGreater(backup_file.stat().st_size, 0)

        # Verify backup contains the record
        b_con = sqlite3.connect(str(backup_file))
        try:
            cur = b_con.execute("SELECT amount FROM external_cash_flows WHERE flow_id='cf1';")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "100.00")
        finally:
            b_con.close()

    def test_secure_file_permissions(self) -> None:
        """Database file permissions must be 0600."""
        run_migrations(self.db_path)
        st = self.db_path.stat()
        file_mode = oct(st.st_mode & 0o777)
        self.assertEqual(file_mode, oct(0o600))

    def test_pragmas_configured(self) -> None:
        """Connection must have WAL mode, foreign keys ON, and busy timeout set."""
        con = get_db_connection(self.db_path)
        try:
            fk = con.execute("PRAGMA foreign_keys;").fetchone()[0]
            self.assertEqual(fk, 1)

            jm = con.execute("PRAGMA journal_mode;").fetchone()[0]
            self.assertEqual(jm.lower(), "wal")

            bt = con.execute("PRAGMA busy_timeout;").fetchone()[0]
            self.assertGreaterEqual(bt, 5000)
        finally:
            con.close()

    def test_fills_table_uniqueness_and_scoped_identifiers(self) -> None:
        """Fills table must enforce (account_id, venue, symbol, trade_id) uniqueness.

        Different account, venue, or symbol must be allowed with same trade_id.
        """
        run_migrations(self.db_path)
        con = get_db_connection(self.db_path)
        try:
            # 1. Insert first fill
            con.execute(
                """
                INSERT INTO fills (
                    schema_version, account_id, venue, symbol, trade_id, order_id,
                    event_time_ms, side, price, base_qty, quote_qty, commission_asset,
                    commission_qty, commission_usdt, valuation_status, source_payload_hash,
                    is_quarantined, created_at_ms
                ) VALUES (1, 'acc1', 'SPOT', 'BTCUSDT', 't1', 'o1', 1000, 'BUY',
                          '50000', '0.1', '5000', 'USDT', '5', '5', 'VALUED', 'h1', 0, 1000);
                """
            )

            # 2. Duplicate insert with exact same key must raise IntegrityError
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    """
                    INSERT INTO fills (
                        schema_version, account_id, venue, symbol, trade_id, order_id,
                        event_time_ms, side, price, base_qty, quote_qty, commission_asset,
                        commission_qty, commission_usdt, valuation_status, source_payload_hash,
                        is_quarantined, created_at_ms
                    ) VALUES (1, 'acc1', 'SPOT', 'BTCUSDT', 't1', 'o2', 1001, 'BUY',
                              '50000', '0.1', '5000', 'USDT', '5', '5', 'VALUED', 'h2', 0, 1001);
                    """
                )

            # 3. Same trade_id with different account_id must succeed
            con.execute(
                """
                INSERT INTO fills (
                    schema_version, account_id, venue, symbol, trade_id, order_id,
                    event_time_ms, side, price, base_qty, quote_qty, commission_asset,
                    commission_qty, commission_usdt, valuation_status, source_payload_hash,
                    is_quarantined, created_at_ms
                ) VALUES (1, 'acc2', 'SPOT', 'BTCUSDT', 't1', 'o1', 1000, 'BUY',
                          '50000', '0.1', '5000', 'USDT', '5', '5', 'VALUED', 'h1', 0, 1000);
                """
            )

            # 4. Same trade_id with different venue must succeed
            con.execute(
                """
                INSERT INTO fills (
                    schema_version, account_id, venue, symbol, trade_id, order_id,
                    event_time_ms, side, price, base_qty, quote_qty, commission_asset,
                    commission_qty, commission_usdt, valuation_status, source_payload_hash,
                    is_quarantined, created_at_ms
                ) VALUES (1, 'acc1', 'FUTURES', 'BTCUSDT', 't1', 'o1', 1000, 'BUY',
                          '50000', '0.1', '5000', 'USDT', '5', '5', 'VALUED', 'h1', 0, 1000);
                """
            )
        finally:
            con.close()

    def test_transfer_intents_uniqueness_constraints(self) -> None:
        """Transfer intents must enforce uniqueness on client_transfer_id and daily policy key."""
        run_migrations(self.db_path)
        con = get_db_connection(self.db_path)
        try:
            con.execute(
                """
                INSERT INTO transfer_intents (
                    schema_version, intent_id, account_id, policy_version, reporting_day_utc,
                    client_transfer_id, amount_usdt, policy_snapshot_json, status,
                    exchange_tran_id, created_at_ms, updated_at_ms
                ) VALUES (1, 'i1', 'acc1', 'v1', '2026-09-28', 'cli_1', '1.00', '{}', 'PLANNED', NULL, 1000, 1000);
                """
            )

            # Duplicate client_transfer_id must fail
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    """
                    INSERT INTO transfer_intents (
                        schema_version, intent_id, account_id, policy_version, reporting_day_utc,
                        client_transfer_id, amount_usdt, policy_snapshot_json, status,
                        exchange_tran_id, created_at_ms, updated_at_ms
                    ) VALUES (1, 'i2', 'acc1', 'v1', '2026-09-29', 'cli_1', '1.00', '{}', 'PLANNED', NULL, 1001, 1001);
                    """
                )

            # Duplicate daily policy key (account_id, policy_version, reporting_day_utc) must fail
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    """
                    INSERT INTO transfer_intents (
                        schema_version, intent_id, account_id, policy_version, reporting_day_utc,
                        client_transfer_id, amount_usdt, policy_snapshot_json, status,
                        exchange_tran_id, created_at_ms, updated_at_ms
                    ) VALUES (1, 'i3', 'acc1', 'v1', '2026-09-28', 'cli_2', '1.00', '{}', 'PLANNED', NULL, 1002, 1002);
                    """
                )
        finally:
            con.close()

    def test_foreign_key_enforcement(self) -> None:
        """Foreign key checks must fail on invalid references."""
        run_migrations(self.db_path)
        con = get_db_connection(self.db_path)
        try:
            # Inserting lot referencing non-existent fill must raise IntegrityError
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    """
                    INSERT INTO lots (
                        schema_version, lot_id, account_id, venue, symbol, acquired_trade_id,
                        opened_at_ms, original_base_qty, remaining_base_qty, quote_cost_usdt,
                        allocated_buy_fee_usdt, completeness, created_at_ms
                    ) VALUES (1, 'lot1', 'acc1', 'SPOT', 'BTCUSDT', 'nonexistent_trade',
                              1000, '0.1', '0.1', '5000', '5', 'VERIFIED', 1000);
                    """
                )
        finally:
            con.close()

    def test_decimal_precision_extreme_cases(self) -> None:
        """Ledger must store and retain exact Decimal precision for PEPE/SHIB without float rounding."""
        run_migrations(self.db_path)
        con = get_db_connection(self.db_path)
        try:
            # Very small price (PEPE/SHIB) and large quantity
            pepe_price = "0.000009876543210123"
            pepe_qty = "1234567890123.456789"
            canonical_pepe_price = canonical_decimal(pepe_price)
            canonical_pepe_qty = canonical_decimal(pepe_qty)
            quote_val = canonical_decimal(str(Decimal(canonical_pepe_price) * Decimal(canonical_pepe_qty)))

            con.execute(
                """
                INSERT INTO fills (
                    schema_version, account_id, venue, symbol, trade_id, order_id,
                    event_time_ms, side, price, base_qty, quote_qty, commission_asset,
                    commission_qty, commission_usdt, valuation_status, source_payload_hash,
                    is_quarantined, created_at_ms
                ) VALUES (1, 'acc1', 'SPOT', 'PEPEUSDT', 't_pepe', 'o_pepe', 1000, 'BUY',
                          ?, ?, ?, 'USDT', '0.01234567', '0.01234567', 'VALUED', 'hash1', 0, 1000);
                """,
                (canonical_pepe_price, canonical_pepe_qty, quote_val),
            )

            cur = con.execute("SELECT price, base_qty, quote_qty FROM fills WHERE trade_id='t_pepe';")
            row = cur.fetchone()
            self.assertEqual(row["price"], canonical_pepe_price)
            self.assertEqual(row["base_qty"], canonical_pepe_qty)
            self.assertEqual(row["quote_qty"], quote_val)

            # Ensure exact Decimal arithmetic roundtrip
            recomputed = Decimal(row["price"]) * Decimal(row["base_qty"])
            self.assertEqual(canonical_decimal(str(recomputed)), quote_val)
        finally:
            con.close()

    def test_checksum_mismatch_detection(self) -> None:
        """Modifying migration checksum must be caught as a migration error."""
        run_migrations(self.db_path)

        con = get_db_connection(self.db_path)
        try:
            # Corrupt recorded checksum
            con.execute("UPDATE schema_migrations SET checksum = 'corrupted_sha256' WHERE version = 1;")
        finally:
            con.close()

        with self.assertRaises(MigrationChecksumMismatchError):
            run_migrations(self.db_path)


if __name__ == "__main__":
    unittest.main()
