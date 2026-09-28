"""Tests for Cost-Aware Capital Rotation policy, ranking, executor state machine, and shadow mode."""

from decimal import Decimal
import unittest
from typing import Any, Dict, List, Mapping, Optional

from hermes.accounting.contracts import (
    Completeness,
    DecimalString,
    RotationAction,
    RotationDecision,
    RotationMarketSnapshot,
    RotationStatus,
    SpotOrderGateway,
    SpotOrderSubmission,
    Venue,
)
from hermes.accounting.repository import (
    SqliteAccountingRepository,
    SqliteRotationRepository,
)
from hermes.accounting.rotation import (
    BinanceSpotOrderGateway,
    RotationExecutor,
    RotationPolicyConfig,
    ShadowRotationRecorder,
    evaluate_rotation,
    rank_rotation_candidates,
)
from hermes.accounting.schema import init_db
from tests.support.isolation import IsolatedTestCase


class FakeSpotOrderGateway:
    """Deterministic fake adhering to SpotOrderGateway protocol."""

    def __init__(self) -> None:
        self.sell_submissions: List[Dict[str, Any]] = []
        self.buy_submissions: List[Dict[str, Any]] = []
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


class TestCostAwareRotation(IsolatedTestCase):
    """Test suite for cost-aware capital rotation pure policy and durable executor."""

    def setUp(self) -> None:
        super().setUp()
        assert self.isolated_dir is not None
        self.db_path = self.isolated_dir / "rotation_test.db"
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
        model_ver: str = "v2",
        schema_ver: str = "v1",
        completeness: Completeness = Completeness.VERIFIED,
        expires_at_ms: int = 1700001000000,
        bars: tuple = ("bar1", "bar2"),
    ) -> RotationMarketSnapshot:
        return RotationMarketSnapshot(
            snapshot_id=f"snap_{symbol}",
            account_id="default",
            venue=Venue.SPOT,
            symbol=symbol,
            position_lifecycle_id=f"pos_{symbol}",
            position_version=1,
            model_version=model_ver,
            feature_schema_version=schema_ver,
            observed_at_ms=1700000000000,
            expires_at_ms=expires_at_ms,
            score=score,
            best_bid_usdt="100",
            best_ask_usdt="100.1",
            spread_fraction=spread,
            available_depth_usdt=depth,
            closed_bar_ids=bars,
            completeness=completeness,
        )

    def test_pure_policy_score_edge_and_pnl_gates(self) -> None:
        """Comparative policy gates: score edge, PnL band, holding time, protected modes."""
        now_ms = 1700000000000
        held_snap = self._make_snapshot("ETHUSDT", score="4")
        cand_snap = self._make_snapshot("SOLUSDT", score="8")

        # 1. Score edge 8 - 4 = 4 >= 3, PnL = -0.01 (within [-0.045, 0.02]), Hold = 2000s -> APPROVE
        dec1 = evaluate_rotation(
            held_snapshot=held_snap, candidate_snapshot=cand_snap,
            held_position_pnl_pct="-0.01", held_holding_time_secs=2000,
            config=self.config, now_ms=now_ms
        )
        self.assertEqual(dec1.action, RotationAction.APPROVE)
        self.assertEqual(dec1.score_edge, "4")

        # 2. Insufficient score edge: candidate score 6 -> edge 2 < 3 -> NO_ROTATION
        cand_low = self._make_snapshot("SOLUSDT", score="6")
        dec2 = evaluate_rotation(
            held_snapshot=held_snap, candidate_snapshot=cand_low,
            held_position_pnl_pct="-0.01", held_holding_time_secs=2000,
            config=self.config, now_ms=now_ms
        )
        self.assertEqual(dec2.action, RotationAction.NO_ROTATION)
        self.assertIn("INSUFFICIENT_SCORE_EDGE", dec2.reason_codes)

        # 3. Position in profit +3.5% > +2.0% -> Protect winners -> NO_ROTATION
        dec3 = evaluate_rotation(
            held_snapshot=held_snap, candidate_snapshot=cand_snap,
            held_position_pnl_pct="0.035", held_holding_time_secs=2000,
            config=self.config, now_ms=now_ms
        )
        self.assertEqual(dec3.action, RotationAction.NO_ROTATION)
        self.assertIn("PNL_OUTSIDE_BAND", dec3.reason_codes)

        # 4. Position holding time < 1800s (e.g. 500s) -> NO_ROTATION
        dec4 = evaluate_rotation(
            held_snapshot=held_snap, candidate_snapshot=cand_snap,
            held_position_pnl_pct="-0.01", held_holding_time_secs=500,
            config=self.config, now_ms=now_ms
        )
        self.assertEqual(dec4.action, RotationAction.NO_ROTATION)
        self.assertIn("MIN_HOLD_NOT_MET", dec4.reason_codes)

        # 5. Position mode is RIDING_TREND -> Protected -> NO_ROTATION
        dec5 = evaluate_rotation(
            held_snapshot=held_snap, candidate_snapshot=cand_snap,
            held_position_pnl_pct="-0.01", held_holding_time_secs=2000,
            config=self.config, now_ms=now_ms, held_mode="RIDING_TREND"
        )
        self.assertEqual(dec5.action, RotationAction.NO_ROTATION)
        self.assertIn("PROTECTED_POSITION", dec5.reason_codes)

    def test_ranking_candidates_deterministic(self) -> None:
        """Candidate ranking sorts by score edge, expected benefit, cost fraction, and symbol."""
        now_ms = 1700000000000
        held1 = self._make_snapshot("ADAUSDT", score="3")
        held2 = self._make_snapshot("DOTUSDT", score="4")
        cand = self._make_snapshot("NEARUSDT", score="9")

        d1 = evaluate_rotation(held1, cand, "-0.01", 2000, self.config, now_ms)
        d2 = evaluate_rotation(held2, cand, "-0.01", 2000, self.config, now_ms)

        ranked = rank_rotation_candidates([d2, d1])
        # d1 has score edge 9 - 3 = 6; d2 has score edge 9 - 4 = 5.
        self.assertEqual(ranked[0].held_symbol, "ADAUSDT")
        self.assertEqual(ranked[1].held_symbol, "DOTUSDT")

    def test_rotation_executor_happy_path_completed(self) -> None:
        """Executor completes sell-then-buy cycle and persists COMPLETED status."""
        now_ms = 1700000000000
        held_snap = self._make_snapshot("ETHUSDT", score="4")
        cand_snap = self._make_snapshot("SOLUSDT", score="8")

        decision = evaluate_rotation(
            held_snapshot=held_snap, candidate_snapshot=cand_snap,
            held_position_pnl_pct="-0.01", held_holding_time_secs=2000,
            config=self.config, now_ms=now_ms
        )

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

        self.assertEqual(intent.status, RotationStatus.COMPLETED)
        self.assertEqual(len(self.gateway.sell_submissions), 1)
        self.assertEqual(len(self.gateway.buy_submissions), 1)
        self.assertEqual(self.gateway.sell_submissions[0]["symbol"], "ETHUSDT")
        self.assertEqual(self.gateway.buy_submissions[0]["symbol"], "SOLUSDT")

    def test_rotation_failed_buy_enters_cash_recovery_retaining_usdt(self) -> None:
        """If replacement buy is rejected/aborted, executor enters CASH_RECOVERY retaining USDT with NO Futures fallback."""
        now_ms = 1700000000000
        held_snap = self._make_snapshot("ETHUSDT", score="4")
        cand_snap = self._make_snapshot("SOLUSDT", score="8")

        decision = evaluate_rotation(
            held_snapshot=held_snap, candidate_snapshot=cand_snap,
            held_position_pnl_pct="-0.01", held_holding_time_secs=2000,
            config=self.config, now_ms=now_ms
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

    def test_shadow_rotation_recorder_zero_gateway_mutations(self) -> None:
        """Shadow recorder evaluates and persists decisions with zero order gateway calls."""
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
        # Verify 0 gateway mutations occurred
        self.assertEqual(len(self.gateway.sell_submissions), 0)
        self.assertEqual(len(self.gateway.buy_submissions), 0)


if __name__ == "__main__":
    unittest.main()
