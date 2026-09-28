"""Unit tests for the Daily Profit Collection model with strict isolation."""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

import hermes.config as cfg
import hermes.api.transfer as transfer_mod
from hermes.api.transfer import (
    track_realized_profit,
    run_daily_profit_collector,
    _today_key,
)
from tests.support.isolation import IsolatedTestCase


class TestDailyProfitCollection(IsolatedTestCase):

    def _state_path(self) -> str:
        return transfer_mod._DAILY_STATE_PATH

    def test_daily_profit_collection_disabled_by_default(self):
        """DAILY_PROFIT_COLLECTION must default to False unless explicitly opted in."""
        self.assertFalse(cfg.DAILY_PROFIT_COLLECTION)
        track_realized_profit("BTC", 2.0)
        with patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf:
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_tf.assert_not_called()

    def test_daily_profit_collection_boolean_env_parsing(self):
        """Test explicit boolean environment parsing for DAILY_PROFIT_COLLECTION."""
        from hermes.config import parse_bool_env
        self.assertFalse(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={}))
        self.assertTrue(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "true"}))
        self.assertTrue(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "1"}))
        self.assertTrue(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "yes"}))
        self.assertTrue(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "True"}))
        self.assertFalse(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "false"}))
        self.assertFalse(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": "0"}))
        self.assertFalse(parse_bool_env("DAILY_PROFIT_COLLECTION", default=False, env_dict={"DAILY_PROFIT_COLLECTION": ""}))

    def test_track_accumulates_profit(self):
        track_realized_profit("BTC", 0.40)
        track_realized_profit("SOL", 0.35)
        state_file = self._state_path()
        self.assertTrue(os.path.exists(state_file))
        with open(state_file) as f:
            state = json.load(f)
        today = _today_key()
        self.assertAlmostEqual(state[today]["collected_profit"], 0.75)
        self.assertFalse(state[today]["target_met"])

    def test_track_includes_losses_net_accounting_contract(self):
        """Contract: Net accounting must record losses rather than ignoring them."""
        track_realized_profit("BTC", -0.50)
        state_file = self._state_path()
        self.assertTrue(os.path.exists(state_file))
        with open(state_file) as f:
            state = json.load(f)
        today = _today_key()
        self.assertAlmostEqual(state[today]["collected_profit"], -0.50)

    def test_no_collection_below_target(self):
        track_realized_profit("BTC", 0.60)
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf:
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_tf.assert_not_called()

    def test_legacy_json_cannot_authorize_transfers_when_cutover_pending(self):
        """Disarming contract: daily_profit_state.json cannot authorize transfers under any flag state."""
        track_realized_profit("BTC", 50.0)  # High profit in legacy state file
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "transfer_spot_to_funding") as mock_tf, \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 100.0}):
            # Without approved accounting cutover and verified ledger surplus, transfer is blocked
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_tf.assert_not_called()

    def test_collection_transfers_when_ledger_surplus_and_cutover_approved(self):
        """SpotToFundingCollector executes transfer when ledger has verified surplus and cutover approved."""
        from hermes.accounting.schema import init_db
        from hermes.accounting.repository import SqliteAccountingRepository
        from hermes.accounting.contracts import (
            AccountingCutover,
            CutoverStatus,
            FillEvent,
            TradeSide,
            ValuationStatus,
            Venue,
        )
        import hermes.config as config_mod

        db_path = getattr(config_mod, "ACCOUNTING_DB_PATH", cfg.ACCOUNTING_DB_PATH)
        init_db(db_path)
        repo = SqliteAccountingRepository(db_path)

        now_ms = 1700000000000
        repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_approved",
                account_id="default",
                venue=Venue.SPOT,
                cutover_at_ms=now_ms - 50000,
                baseline_reference="test",
                backfill_from_ms=None,
                backfill_through_ms=now_ms - 50000,
                status=CutoverStatus.APPROVED,
                approved_at_ms=now_ms - 50000,
            )
        )

        # Ingest buy then profitable sell
        repo.ingest_fill(FillEvent(
            schema_version=1, account_id="default", venue=Venue.SPOT, symbol="SOLUSDT",
            trade_id="t_buy_1", order_id="ord_1", event_time_ms=now_ms - 20000,
            side=TradeSide.BUY, price="100", base_qty="1", quote_qty="100",
            commission_asset="USDT", commission_qty="0", commission_usdt="0",
            valuation_status=ValuationStatus.VALUED, source_payload_hash="h1"
        ))
        repo.ingest_fill(FillEvent(
            schema_version=1, account_id="default", venue=Venue.SPOT, symbol="SOLUSDT",
            trade_id="t_sell_1", order_id="ord_2", event_time_ms=now_ms - 10000,
            side=TradeSide.SELL, price="102", base_qty="1", quote_qty="102",
            commission_asset="USDT", commission_qty="0", commission_usdt="0",
            valuation_status=ValuationStatus.VALUED, source_payload_hash="h2"
        ))
        from hermes.accounting.contracts import FillKey
        repo.apply_fifo_sell(FillKey("default", Venue.SPOT, "SOLUSDT", "t_sell_1"))

        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch("hermes.api.auth.binance_signed_request", return_value={"tranId": 456}), \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 50.0}), \
             patch.object(transfer_mod, "telegram_send"):
            result = run_daily_profit_collector()
        self.assertTrue(result)

    def test_no_double_collection_after_target_met(self):
        """SpotToFundingCollector prevents duplicate daily transfers."""
        from hermes.accounting.schema import init_db
        from hermes.accounting.repository import SqliteAccountingRepository
        from hermes.accounting.contracts import (
            AccountingCutover,
            CutoverStatus,
            FillEvent,
            TradeSide,
            ValuationStatus,
            Venue,
            FillKey,
        )
        import hermes.config as config_mod

        db_path = getattr(config_mod, "ACCOUNTING_DB_PATH", cfg.ACCOUNTING_DB_PATH)
        init_db(db_path)
        repo = SqliteAccountingRepository(db_path)

        now_ms = 1700000000000
        repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_approved",
                account_id="default",
                venue=Venue.SPOT,
                cutover_at_ms=now_ms - 50000,
                baseline_reference="test",
                backfill_from_ms=None,
                backfill_through_ms=now_ms - 50000,
                status=CutoverStatus.APPROVED,
                approved_at_ms=now_ms - 50000,
            )
        )

        repo.ingest_fill(FillEvent(
            schema_version=1, account_id="default", venue=Venue.SPOT, symbol="SOLUSDT",
            trade_id="t_buy_1", order_id="ord_1", event_time_ms=now_ms - 20000,
            side=TradeSide.BUY, price="100", base_qty="1", quote_qty="100",
            commission_asset="USDT", commission_qty="0", commission_usdt="0",
            valuation_status=ValuationStatus.VALUED, source_payload_hash="h1"
        ))
        repo.ingest_fill(FillEvent(
            schema_version=1, account_id="default", venue=Venue.SPOT, symbol="SOLUSDT",
            trade_id="t_sell_1", order_id="ord_2", event_time_ms=now_ms - 10000,
            side=TradeSide.SELL, price="105", base_qty="1", quote_qty="105",
            commission_asset="USDT", commission_qty="0", commission_usdt="0",
            valuation_status=ValuationStatus.VALUED, source_payload_hash="h2"
        ))
        repo.apply_fifo_sell(FillKey("default", Venue.SPOT, "SOLUSDT", "t_sell_1"))

        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch("hermes.api.auth.binance_signed_request", return_value={"tranId": 123}), \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 50.0}), \
             patch.object(transfer_mod, "telegram_send"):
            self.assertTrue(run_daily_profit_collector())

        # Second attempt on same day must be rejected by claim idempotency
        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 50.0}), \
             patch("hermes.api.auth.binance_signed_request") as mock_tf2:
            self.assertFalse(run_daily_profit_collector())
            mock_tf2.assert_not_called()

    def test_collection_deferred_when_spot_low(self):
        """Spot balance below target defers collection."""
        from hermes.accounting.schema import init_db
        from hermes.accounting.repository import SqliteAccountingRepository
        from hermes.accounting.contracts import (
            AccountingCutover,
            CutoverStatus,
            FillEvent,
            TradeSide,
            ValuationStatus,
            Venue,
            FillKey,
        )
        import hermes.config as config_mod

        db_path = getattr(config_mod, "ACCOUNTING_DB_PATH", cfg.ACCOUNTING_DB_PATH)
        init_db(db_path)
        repo = SqliteAccountingRepository(db_path)

        now_ms = 1700000000000
        repo.create_cutover(
            AccountingCutover(
                cutover_id="cutover_approved",
                account_id="default",
                venue=Venue.SPOT,
                cutover_at_ms=now_ms - 50000,
                baseline_reference="test",
                backfill_from_ms=None,
                backfill_through_ms=now_ms - 50000,
                status=CutoverStatus.APPROVED,
                approved_at_ms=now_ms - 50000,
            )
        )

        repo.ingest_fill(FillEvent(
            schema_version=1, account_id="default", venue=Venue.SPOT, symbol="SOLUSDT",
            trade_id="t_buy_1", order_id="ord_1", event_time_ms=now_ms - 20000,
            side=TradeSide.BUY, price="100", base_qty="1", quote_qty="100",
            commission_asset="USDT", commission_qty="0", commission_usdt="0",
            valuation_status=ValuationStatus.VALUED, source_payload_hash="h1"
        ))
        repo.ingest_fill(FillEvent(
            schema_version=1, account_id="default", venue=Venue.SPOT, symbol="SOLUSDT",
            trade_id="t_sell_1", order_id="ord_2", event_time_ms=now_ms - 10000,
            side=TradeSide.SELL, price="102", base_qty="1", quote_qty="102",
            commission_asset="USDT", commission_qty="0", commission_usdt="0",
            valuation_status=ValuationStatus.VALUED, source_payload_hash="h2"
        ))
        repo.apply_fifo_sell(FillKey("default", Venue.SPOT, "SOLUSDT", "t_sell_1"))

        with patch.object(cfg, "DAILY_PROFIT_COLLECTION", True), \
             patch.object(transfer_mod, "DAILY_PROFIT_COLLECTION", True), \
             patch("hermes.api.balance.get_balance", return_value={"usdt": 0.50}), \
             patch("hermes.api.auth.binance_signed_request") as mock_req:
            result = run_daily_profit_collector()
        self.assertFalse(result)
        mock_req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
