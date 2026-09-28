"""Unit tests for Phase 2 Accounting Ops, Rehearsal, and CLI subcommands."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hermes.accounting.ops import (
    init_isolated_db,
    verify_db,
    backup_accounting_state,
    restore_accounting_state,
    rehearse_reconciliation,
    get_accounting_status,
)
from hermes.cli import main
from tests.support.isolation import IsolatedTestCase


class TestAccountingOpsAndCli(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        self.test_dir = Path(tempfile.mkdtemp(prefix="acc_ops_test_"))
        self.db_path = self.test_dir / "test_ledger.db"

    def tearDown(self):
        super().tearDown()

    def test_init_and_verify_isolated_db(self):
        init_res = init_isolated_db(self.db_path)
        self.assertEqual(init_res["status"], "INITIALIZED")
        self.assertTrue(self.db_path.exists())
        self.assertEqual(init_res["journal_mode"], "WAL")
        self.assertEqual(init_res["cutover_status"], "PENDING")

        verify_res = verify_db(self.db_path)
        self.assertEqual(verify_res["status"], "HEALTHY")
        self.assertTrue(verify_res["permission_ok"])
        self.assertEqual(len(verify_res["errors"]), 0)

    def test_backup_and_restore_db(self):
        init_isolated_db(self.db_path)
        backup_dir = self.test_dir / "backups"
        bak_res = backup_accounting_state(self.db_path, backup_dir=backup_dir)
        self.assertEqual(bak_res["status"], "BACKUP_COMPLETED")
        self.assertIsNotNone(bak_res["db_backup_path"])
        self.assertTrue(Path(bak_res["db_backup_path"]).exists())

        # Restore into another location
        restore_db_path = self.test_dir / "restored_ledger.db"
        res_res = restore_accounting_state(bak_res["db_backup_path"], restore_db_path)
        self.assertEqual(res_res["status"], "RESTORED")
        self.assertTrue(restore_db_path.exists())

        verify_res = verify_db(restore_db_path)
        self.assertEqual(verify_res["status"], "HEALTHY")

    def test_rehearsal_with_archived_trades(self):
        sample_fixture = Path(__file__).resolve().parent.parent / "fixtures" / "archived_trades_sample.json"
        rehearsal_db = self.test_dir / "rehearsal.db"
        res = rehearse_reconciliation(rehearsal_db, trades_file=sample_fixture, dry_run=True)

        self.assertEqual(res["status"], "REHEARSAL_SUCCESS")
        self.assertEqual(res["input_trades_count"], 9)
        self.assertGreaterEqual(res["inserted_fills"], 8)
        self.assertGreaterEqual(res["sell_outcomes_count"], 4)
        self.assertEqual(res["accounting_snapshot"]["cutover_status"], "PENDING")
        self.assertEqual(res["distribution_policy_evaluation"]["action"], "BLOCKED")
        self.assertEqual(res["rotation_policy_evaluation"]["order_mutations_executed"], 0)

        status_res = get_accounting_status(rehearsal_db)
        self.assertEqual(status_res["status"], "ACTIVE")
        self.assertEqual(status_res["cutover_status"], "PENDING")

    def test_verify_missing_db(self):
        missing_db = self.test_dir / "nonexistent.db"
        res = verify_db(missing_db)
        self.assertEqual(res["status"], "NOT_FOUND")
        self.assertIn("does not exist", res["errors"][0])

    def test_restore_missing_backup_raises_error(self):
        missing_bak = self.test_dir / "nonexistent.bak"
        with self.assertRaises(FileNotFoundError):
            restore_accounting_state(missing_bak, self.db_path)

    def test_cli_subcommands_end_to_end(self):
        cli_db = self.test_dir / "cli_test.db"
        backup_dir = self.test_dir / "cli_backups"
        sample_fixture = Path(__file__).resolve().parent.parent / "fixtures" / "archived_trades_sample.json"

        # 1. accounting-init
        with patch.object(sys, "argv", ["hermes", "accounting-init", "--db-path", str(cli_db)]):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                main()
                data = json.loads(out.getvalue())
                self.assertEqual(data["status"], "INITIALIZED")

        # 2. accounting-verify
        with patch.object(sys, "argv", ["hermes", "accounting-verify", "--db-path", str(cli_db)]):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                main()
                data = json.loads(out.getvalue())
                self.assertEqual(data["status"], "HEALTHY")

        # 3. accounting-backup
        with patch.object(sys, "argv", ["hermes", "accounting-backup", "--db-path", str(cli_db), "--backup-dir", str(backup_dir)]):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                main()
                bak_data = json.loads(out.getvalue())
                self.assertEqual(bak_data["status"], "BACKUP_COMPLETED")
                backup_file = bak_data["db_backup_path"]

        # 4. accounting-restore
        restore_cli_db = self.test_dir / "cli_restored.db"
        with patch.object(sys, "argv", ["hermes", "accounting-restore", "--backup-file", backup_file, "--db-path", str(restore_cli_db)]):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                main()
                res_data = json.loads(out.getvalue())
                self.assertEqual(res_data["status"], "RESTORED")

        # 5. accounting-reconcile
        reconcile_db = self.test_dir / "cli_reconcile.db"
        with patch.object(sys, "argv", ["hermes", "accounting-reconcile", "--db-path", str(reconcile_db), "--trades-file", str(sample_fixture)]):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                main()
                rec_data = json.loads(out.getvalue())
                self.assertEqual(rec_data["status"], "REHEARSAL_SUCCESS")
                self.assertEqual(rec_data["sell_outcomes_count"], 4)

        # 6. accounting-status
        with patch.object(sys, "argv", ["hermes", "accounting-status", "--db-path", str(reconcile_db)]):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                main()
                status_data = json.loads(out.getvalue())
                self.assertEqual(status_data["status"], "ACTIVE")
                self.assertEqual(status_data["cutover_status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
