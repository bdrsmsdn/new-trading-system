"""
Sentinel unit tests verifying the test isolation and safety foundation.

Guarantees:
1. No test execution can delete or write to production state/ledger files.
2. All persistent paths point to an isolated temporary directory under $TMPDIR.
3. Unmocked network access (sockets, urllib, requests, subprocess curl) is globally blocked.
4. Telegram alerts cannot make external requests without explicit mocking.
5. Singleton application state is cleanly snapshotted and restored.
"""

import copy
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    requests = None  # type: ignore
    _HAS_REQUESTS = False

import hermes.config as cfg
import hermes.state as state_mod
import hermes.api.transfer as transfer_mod
from hermes.notifications.telegram import telegram_send
from tests.support.isolation import (
    IsolatedTestCase,
    NetworkAccessBlockedError,
    ProductionFileAccessError,
    isolate_test_environment,
)


class TestTestIsolation(IsolatedTestCase):

    def test_isolated_dir_under_tmpdir(self):
        """Temporary isolated directory must be created inside TMPDIR / scratch space."""
        self.assertIsNotNone(self.isolated_dir)
        assert self.isolated_dir is not None
        tmpdir = os.environ.get("TMPDIR", tempfile.gettempdir())
        isolated_dir_str = str(self.isolated_dir.resolve())
        tmpdir_str = str(pathlib.Path(tmpdir).resolve())
        self.assertTrue(
            isolated_dir_str.startswith(tmpdir_str),
            f"Isolated dir {isolated_dir_str} not under tmpdir {tmpdir_str}"
        )

    def test_module_paths_redirected(self):
        """State paths across hermes modules must point inside isolated temp directory."""
        self.assertIsNotNone(self.isolated_dir)
        assert self.isolated_dir is not None
        iso_str = str(self.isolated_dir.resolve())

        self.assertTrue(str(pathlib.Path(cfg.STATE_FILE).resolve()).startswith(iso_str))
        self.assertTrue(str(pathlib.Path(cfg.PRICE_CACHE).resolve()).startswith(iso_str))
        self.assertTrue(str(pathlib.Path(cfg.AGENT_MEMORY_FILE).resolve()).startswith(iso_str))
        self.assertTrue(str(pathlib.Path(cfg.PID_FILE).resolve()).startswith(iso_str))
        if hasattr(cfg, "LEDGER_PATH"):
            ledger_path = getattr(cfg, "LEDGER_PATH")
            self.assertTrue(str(pathlib.Path(ledger_path).resolve()).startswith(iso_str))

        self.assertTrue(str(pathlib.Path(state_mod.STATE_FILE).resolve()).startswith(iso_str))
        self.assertTrue(str(pathlib.Path(transfer_mod._DAILY_STATE_PATH).resolve()).startswith(iso_str))

    def test_production_file_deletion_blocked(self):
        """Attempting to delete a production state file outside tempdir must raise ProductionFileAccessError."""
        fake_prod_path = "/var/www/new-trading-system/daily_profit_state.json"
        with self.assertRaises(ProductionFileAccessError):
            os.remove(fake_prod_path)

        with self.assertRaises(ProductionFileAccessError):
            os.unlink(fake_prod_path)

        with self.assertRaises(ProductionFileAccessError):
            pathlib.Path(fake_prod_path).unlink()

    def test_production_file_write_blocked(self):
        """Attempting to open a production state file for writing must raise ProductionFileAccessError."""
        fake_prod_path = "/var/www/new-trading-system/hermes_trader_state.json"
        with self.assertRaises(ProductionFileAccessError):
            open(fake_prod_path, "w")

        with self.assertRaises(ProductionFileAccessError):
            open(fake_prod_path, "a")

    def test_temp_file_operations_allowed(self):
        """Writing and deleting files inside the isolated temp directory must succeed."""
        self.assertIsNotNone(self.isolated_dir)
        assert self.isolated_dir is not None
        test_file = self.isolated_dir / "daily_profit_state.json"
        with open(test_file, "w") as f:
            f.write('{"test": true}')

        self.assertTrue(test_file.exists())
        os.remove(str(test_file))
        self.assertFalse(test_file.exists())

    def test_socket_connect_blocked(self):
        """Unmocked socket connect must be blocked with NetworkAccessBlockedError."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(NetworkAccessBlockedError):
                s.connect(("api.binance.com", 443))
        finally:
            s.close()

    def test_socket_create_connection_blocked(self):
        """Unmocked socket.create_connection must be blocked with NetworkAccessBlockedError."""
        with self.assertRaises(NetworkAccessBlockedError):
            socket.create_connection(("8.8.8.8", 53))

    def test_urllib_urlopen_blocked(self):
        """Unmocked urllib.request.urlopen must be blocked with NetworkAccessBlockedError."""
        with self.assertRaises(NetworkAccessBlockedError):
            urllib.request.urlopen("https://api.binance.com/api/v3/time")

    def test_requests_blocked_if_installed(self):
        """Unmocked requests calls must be blocked with NetworkAccessBlockedError."""
        if _HAS_REQUESTS and requests is not None:
            with self.assertRaises(NetworkAccessBlockedError):
                requests.get("https://api.telegram.org")
            with self.assertRaises(NetworkAccessBlockedError):
                requests.post("https://api.binance.com/api/v3/order")

    def test_subprocess_curl_blocked(self):
        """Unmocked subprocess curl execution must be blocked with NetworkAccessBlockedError."""
        with self.assertRaises(NetworkAccessBlockedError):
            subprocess.run(["curl", "-s", "https://api.binance.com/api/v3/time"])

        with self.assertRaises(NetworkAccessBlockedError):
            subprocess.Popen(["curl", "https://api.binance.com/api/v3/time"])

    def test_telegram_send_blocked_when_unmocked(self):
        """Calling telegram_send with active credentials without mock is blocked."""
        with patch("hermes.notifications.telegram._telegram_enabled", True), \
             patch("hermes.notifications.telegram.TELEGRAM_BOT_TOKEN", "mock_token"), \
             patch("hermes.notifications.telegram.TELEGRAM_CHAT_ID", "mock_chat_id"):
            # telegram_send internally catches Exception and returns False when network call is blocked
            result = telegram_send("Test message sentinel")
            self.assertFalse(result)

    def test_singleton_state_restoration(self):
        """Singleton modifications made during isolated scope are restored."""
        orig_positions = copy.deepcopy(state_mod.state.positions)
        orig_fg = state_mod.state.fg_value

        with isolate_test_environment():
            state_mod.state.positions["ISOLATION_TEST"] = {
                "entry_price": 999.0,
                "qty": 1.0,
            }
            state_mod.state.fg_value = 99
            self.assertIn("ISOLATION_TEST", state_mod.state.positions)
            self.assertEqual(state_mod.state.fg_value, 99)

        # After exiting isolate_test_environment, state must be restored
        self.assertEqual(state_mod.state.positions, orig_positions)
        self.assertEqual(state_mod.state.fg_value, orig_fg)


if __name__ == "__main__":
    unittest.main()
