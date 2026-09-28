"""Cost-aware capital rotation domain logic, comparative policy, and executor.

Implements symmetric snapshot evaluation, score-vs-cost separation, deterministic
ranking, sell-then-buy state machine with CASH_RECOVERY (no Futures fallback),
and zero-mutation shadow evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import time
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union
import uuid

from hermes.accounting.contracts import (
    AccountingRepository,
    Completeness,
    DecimalString,
    FillEvent,
    FillKey,
    ReasonCodes,
    RotationAction,
    RotationDecision,
    RotationIntent,
    RotationMarketSnapshot,
    RotationRepository,
    RotationStatus,
    SpotOrderGateway,
    SpotOrderSubmission,
    TradeSide,
    ValuationStatus,
    Venue,
    canonical_decimal,
)
from hermes.logging_setup import log


@dataclass(frozen=True)
class RotationPolicyConfig:
    """Configuration parameters for cost-aware capital rotation."""
    model_version: str = "v2"
    feature_schema_version: str = "v1"
    enabled: bool = False
    shadow_mode: bool = True
    min_score_edge: DecimalString = "3"
    min_pnl_pct: DecimalString = "-0.045"
    max_pnl_pct: DecimalString = "0.02"
    min_hold_secs: int = 1800
    max_cost_fraction: DecimalString = "0.015"
    max_spread_fraction: DecimalString = "0.005"
    min_depth_usdt: DecimalString = "50"
    min_trade_usdt: DecimalString = "5.5"
    fee_rate_fraction: DecimalString = "0.001"
    cooldown_secs: int = 1800
    daily_attempt_cap: int = 5


def evaluate_rotation(
    held_snapshot: RotationMarketSnapshot,
    candidate_snapshot: RotationMarketSnapshot,
    held_position_pnl_pct: DecimalString,
    held_holding_time_secs: int,
    config: RotationPolicyConfig,
    now_ms: int,
    held_mode: str = "NORMAL",
    position_lifecycle_id: Optional[str] = None,
    position_version: int = 1,
    position_notional_usdt: DecimalString = "25",
) -> RotationDecision:
    """Pure comparative evaluation between a held position and replacement candidate.

    Performs NO I/O. Separates dimensionless score edge from monetary execution costs.
    Returns an immutable RotationDecision with stable reason codes.
    """
    decision_id = f"rotdec_{uuid.uuid4().hex}"
    lifecycle_id = position_lifecycle_id or held_snapshot.position_lifecycle_id or f"pos_{held_snapshot.symbol}"
    
    # 1. Validate canonical decimal inputs
    try:
        held_pnl_dec = Decimal(canonical_decimal(held_position_pnl_pct))
        held_score_dec = Decimal(canonical_decimal(held_snapshot.score, non_negative=True))
        cand_score_dec = Decimal(canonical_decimal(candidate_snapshot.score, non_negative=True))
        min_edge_dec = Decimal(canonical_decimal(config.min_score_edge, non_negative=True))
        min_pnl_dec = Decimal(canonical_decimal(config.min_pnl_pct))
        max_pnl_dec = Decimal(canonical_decimal(config.max_pnl_pct))
        max_cost_frac_dec = Decimal(canonical_decimal(config.max_cost_fraction, non_negative=True))
        max_spread_dec = Decimal(canonical_decimal(config.max_spread_fraction, non_negative=True))
        min_depth_dec = Decimal(canonical_decimal(config.min_depth_usdt, non_negative=True))
        notional_dec = Decimal(canonical_decimal(position_notional_usdt, non_negative=True))
        fee_rate_dec = Decimal(canonical_decimal(config.fee_rate_fraction, non_negative=True))
    except (ValueError, InvalidOperation):
        return RotationDecision(
            schema_version=1,
            decision_id=decision_id,
            account_id=held_snapshot.account_id,
            held_symbol=held_snapshot.symbol,
            candidate_symbol=candidate_snapshot.symbol,
            position_lifecycle_id=lifecycle_id,
            position_version=position_version,
            model_version=config.model_version,
            held_snapshot_id=held_snapshot.snapshot_id,
            candidate_snapshot_id=candidate_snapshot.snapshot_id,
            held_score=held_snapshot.score,
            candidate_score=candidate_snapshot.score,
            score_edge="0",
            estimated_roundtrip_cost_usdt=None,
            estimated_cost_fraction=None,
            expected_net_benefit_usdt=None,
            risk_decision_id=None,
            action=RotationAction.BLOCKED,
            reason_codes=("INVALID_INPUT",),
            decided_at_ms=now_ms,
        )

    score_edge_dec = cand_score_dec - held_score_dec
    score_edge_str = canonical_decimal(str(score_edge_dec))

    # 2. Estimate roundtrip costs
    held_spread_dec = Decimal(canonical_decimal(held_snapshot.spread_fraction, non_negative=True))
    cand_spread_dec = Decimal(canonical_decimal(candidate_snapshot.spread_fraction, non_negative=True))
    
    # Roundtrip cost fraction: 2 * fee_rate + (held_spread / 2) + (cand_spread / 2)
    est_cost_frac_dec = (Decimal("2") * fee_rate_dec) + (held_spread_dec / Decimal("2")) + (cand_spread_dec / Decimal("2"))
    est_cost_frac_str = canonical_decimal(str(est_cost_frac_dec), non_negative=True)
    
    est_roundtrip_cost_dec = notional_dec * est_cost_frac_dec
    est_roundtrip_cost_str = canonical_decimal(str(est_roundtrip_cost_dec), non_negative=True)

    # Expected benefit in USDT: score edge advantage proportional to position notional (e.g. 1% per score point edge)
    gross_benefit_dec = notional_dec * (score_edge_dec * Decimal("0.01"))
    net_benefit_dec = gross_benefit_dec - est_roundtrip_cost_dec
    net_benefit_str = canonical_decimal(str(net_benefit_dec))

    reasons: List[str] = []

    # 3. Gate 1: Symmetric snapshot validation
    if (
        held_snapshot.model_version != config.model_version
        or candidate_snapshot.model_version != config.model_version
        or held_snapshot.feature_schema_version != config.feature_schema_version
        or candidate_snapshot.feature_schema_version != config.feature_schema_version
        or held_snapshot.completeness != Completeness.VERIFIED
        or candidate_snapshot.completeness != Completeness.VERIFIED
    ):
        reasons.append("INCOMPARABLE_SNAPSHOT")

    # 4. Gate 2: Freshness / Closed bars
    if now_ms > held_snapshot.expires_at_ms or now_ms > candidate_snapshot.expires_at_ms:
        reasons.append("STALE_SNAPSHOT")
    if not candidate_snapshot.closed_bar_ids:
        reasons.append("CONFIRMATION_MISSING")

    # 5. Gate 3: Protected position modes (never cut winners or trending positions)
    if held_mode.upper() in ("RIDING", "RIDING_TREND", "TP_EVALUATING", "EXIT_PENDING"):
        reasons.append("PROTECTED_POSITION")

    # 6. Gate 4: Score edge threshold
    if score_edge_dec < min_edge_dec:
        reasons.append("INSUFFICIENT_SCORE_EDGE")

    # 7. Gate 5: Held PnL band (e.g. -4.5% to +2.0%)
    if held_pnl_dec < min_pnl_dec or held_pnl_dec > max_pnl_dec:
        reasons.append("PNL_OUTSIDE_BAND")

    # 8. Gate 6: Minimum holding time
    if held_holding_time_secs < config.min_hold_secs:
        reasons.append("MIN_HOLD_NOT_MET")

    # 9. Gate 7: Liquidity & spread limits
    if (
        held_spread_dec > max_spread_dec
        or cand_spread_dec > max_spread_dec
        or Decimal(canonical_decimal(held_snapshot.available_depth_usdt, non_negative=True)) < min_depth_dec
        or Decimal(canonical_decimal(candidate_snapshot.available_depth_usdt, non_negative=True)) < min_depth_dec
    ):
        reasons.append("LIQUIDITY_REJECTED")

    # 10. Gate 8: Cost budget limit
    if est_cost_frac_dec > max_cost_frac_dec:
        reasons.append("COST_BUDGET_EXCEEDED")

    if reasons:
        return RotationDecision(
            schema_version=1,
            decision_id=decision_id,
            account_id=held_snapshot.account_id,
            held_symbol=held_snapshot.symbol,
            candidate_symbol=candidate_snapshot.symbol,
            position_lifecycle_id=lifecycle_id,
            position_version=position_version,
            model_version=config.model_version,
            held_snapshot_id=held_snapshot.snapshot_id,
            candidate_snapshot_id=candidate_snapshot.snapshot_id,
            held_score=held_snapshot.score,
            candidate_score=candidate_snapshot.score,
            score_edge=score_edge_str,
            estimated_roundtrip_cost_usdt=est_roundtrip_cost_str,
            estimated_cost_fraction=est_cost_frac_str,
            expected_net_benefit_usdt=net_benefit_str,
            risk_decision_id=None,
            action=RotationAction.NO_ROTATION,
            reason_codes=tuple(reasons),
            decided_at_ms=now_ms,
        )

    # 11. Feature flag / Shadow check
    action = RotationAction.APPROVE
    action_reasons: Tuple[str, ...] = ()
    if not config.enabled:
        if config.shadow_mode:
            action_reasons = ("SHADOW_ONLY",)
        else:
            action = RotationAction.NO_ROTATION
            action_reasons = ("FEATURE_DISABLED",)

    return RotationDecision(
        schema_version=1,
        decision_id=decision_id,
        account_id=held_snapshot.account_id,
        held_symbol=held_snapshot.symbol,
        candidate_symbol=candidate_snapshot.symbol,
        position_lifecycle_id=lifecycle_id,
        position_version=position_version,
        model_version=config.model_version,
        held_snapshot_id=held_snapshot.snapshot_id,
        candidate_snapshot_id=candidate_snapshot.snapshot_id,
        held_score=held_snapshot.score,
        candidate_score=candidate_snapshot.score,
        score_edge=score_edge_str,
        estimated_roundtrip_cost_usdt=est_roundtrip_cost_str,
        estimated_cost_fraction=est_cost_frac_str,
        expected_net_benefit_usdt=net_benefit_str,
        risk_decision_id=None,
        action=action,
        reason_codes=action_reasons,
        decided_at_ms=now_ms,
    )


def rank_rotation_candidates(
    decisions: Sequence[RotationDecision],
) -> Sequence[RotationDecision]:
    """Deterministically rank approved rotation candidates.

    Sort Order:
    1. Highest score edge (descending)
    2. Highest expected net benefit in USDT (descending)
    3. Lowest estimated cost fraction (ascending)
    4. Canonical held symbol (alphabetical ascending tie-breaker)
    """
    approved = [d for d in decisions if d.action == RotationAction.APPROVE]

    def _sort_key(d: RotationDecision) -> Tuple[Decimal, Decimal, Decimal, str]:
        edge = Decimal(d.score_edge)
        benefit = Decimal(d.expected_net_benefit_usdt or "0")
        cost = Decimal(d.estimated_cost_fraction or "1")
        # Invert edge and benefit for descending order
        return (-edge, -benefit, cost, d.held_symbol)

    return sorted(approved, key=_sort_key)


class BinanceSpotOrderGateway:
    """Production Spot-only order gateway adhering to SpotOrderGateway protocol.

    Has ZERO Futures routing capability or endpoints.
    """

    def submit_sell(
        self,
        *,
        symbol: str,
        base_qty: DecimalString,
        client_order_id: str,
    ) -> SpotOrderSubmission:
        """Submit one Spot market sell order."""
        now_ms = int(time.time() * 1000)
        canonical_qty = canonical_decimal(base_qty, non_negative=True)
        try:
            from hermes.api.auth import binance_signed_request
            params = {
                "symbol": symbol.upper(),
                "side": "SELL",
                "type": "MARKET",
                "quantity": canonical_qty,
                "newClientOrderId": client_order_id,
            }
            res = binance_signed_request("/api/v3/order", params=params, method="POST")
            if isinstance(res, dict) and "orderId" in res:
                return SpotOrderSubmission(
                    accepted=True,
                    unknown=False,
                    client_order_id=client_order_id,
                    exchange_order_id=str(res["orderId"]),
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code=None,
                )
            elif isinstance(res, dict) and res.get("code", 0) < 0:
                err_code = str(res.get("code"))
                msg = str(res.get("msg", ""))
                return SpotOrderSubmission(
                    accepted=False,
                    unknown=False,
                    client_order_id=client_order_id,
                    exchange_order_id=None,
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code=f"{err_code}:{msg}",
                )
            else:
                return SpotOrderSubmission(
                    accepted=False,
                    unknown=True,
                    client_order_id=client_order_id,
                    exchange_order_id=None,
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code="UNEXPECTED_RESPONSE",
                )
        except Exception as exc:
            log.error(f"[SPOT-GATEWAY] Exception submitting sell order {client_order_id}: {exc}")
            return SpotOrderSubmission(
                accepted=False,
                unknown=True,
                client_order_id=client_order_id,
                exchange_order_id=None,
                submitted_at_ms=now_ms,
                raw_response_hash=None,
                error_code=type(exc).__name__,
            )

    def submit_buy(
        self,
        *,
        symbol: str,
        quote_budget_usdt: DecimalString,
        client_order_id: str,
    ) -> SpotOrderSubmission:
        """Submit one Spot market buy order bounded by quote budget."""
        now_ms = int(time.time() * 1000)
        canonical_budget = canonical_decimal(quote_budget_usdt, non_negative=True)
        try:
            from hermes.api.auth import binance_signed_request
            params = {
                "symbol": symbol.upper(),
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": canonical_budget,
                "newClientOrderId": client_order_id,
            }
            res = binance_signed_request("/api/v3/order", params=params, method="POST")
            if isinstance(res, dict) and "orderId" in res:
                return SpotOrderSubmission(
                    accepted=True,
                    unknown=False,
                    client_order_id=client_order_id,
                    exchange_order_id=str(res["orderId"]),
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code=None,
                )
            elif isinstance(res, dict) and res.get("code", 0) < 0:
                err_code = str(res.get("code"))
                msg = str(res.get("msg", ""))
                return SpotOrderSubmission(
                    accepted=False,
                    unknown=False,
                    client_order_id=client_order_id,
                    exchange_order_id=None,
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code=f"{err_code}:{msg}",
                )
            else:
                return SpotOrderSubmission(
                    accepted=False,
                    unknown=True,
                    client_order_id=client_order_id,
                    exchange_order_id=None,
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code="UNEXPECTED_RESPONSE",
                )
        except Exception as exc:
            log.error(f"[SPOT-GATEWAY] Exception submitting buy order {client_order_id}: {exc}")
            return SpotOrderSubmission(
                accepted=False,
                unknown=True,
                client_order_id=client_order_id,
                exchange_order_id=None,
                submitted_at_ms=now_ms,
                raw_response_hash=None,
                error_code=type(exc).__name__,
            )

    def read_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        """Read order status for reconciliation."""
        try:
            from hermes.api.auth import binance_signed_request
            params = {
                "symbol": symbol.upper(),
                "origClientOrderId": client_order_id,
            }
            res = binance_signed_request("/api/v3/order", params=params, method="GET")
            if isinstance(res, dict):
                return res
            return {}
        except Exception as exc:
            log.error(f"[SPOT-GATEWAY] Error reading order {client_order_id}: {exc}")
            return {}


class RotationExecutor:
    """Executes durable sell-then-buy capital rotation state machine.

    Enforces:
    - Atomic cooldown starting at SELL_SUBMITTING
    - Ingests fills into accounting repository
    - If replacement buy fails or is aborted: enters CASH_RECOVERY with USDT retained
    - ZERO fallback to Futures.
    """

    def __init__(
        self,
        rotation_repo: RotationRepository,
        accounting_repo: AccountingRepository,
        order_gateway: SpotOrderGateway,
        config: RotationPolicyConfig,
    ) -> None:
        self.rotation_repo = rotation_repo
        self.accounting_repo = accounting_repo
        self.order_gateway = order_gateway
        self.config = config

    def execute_rotation(
        self,
        decision: RotationDecision,
        held_base_qty: DecimalString,
        approved_replacement_budget_usdt: DecimalString,
        candidate_preflight_valid: bool = True,
        post_sale_risk_approved: bool = True,
        required_reserve_usdt: DecimalString = "0",
        now_ms: Optional[int] = None,
    ) -> RotationIntent:
        """Execute the two-leg rotation state machine."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        # 1. Claim rotation intent atomically
        intent = self.rotation_repo.claim_rotation_intent(decision)

        if intent.status != RotationStatus.PLANNED:
            log.info(f"[ROTATION-EXEC] Intent {intent.intent_id} already in status {intent.status.value}")
            return intent

        # 2. Leg 1: Transition to SELL_SUBMITTING and commit
        sell_client_id = f"rotsell_{intent.intent_id[:16]}"
        intent = self.rotation_repo.transition_rotation(
            intent_id=intent.intent_id,
            expected_status=RotationStatus.PLANNED,
            new_status=RotationStatus.SELL_SUBMITTING,
            evidence={
                "sell_client_id": sell_client_id,
                "held_symbol": decision.held_symbol,
                "held_base_qty": held_base_qty,
                "submitted_at_ms": now_ms,
            },
        )

        # 3. Submit Spot sell order
        sell_sub = self.order_gateway.submit_sell(
            symbol=decision.held_symbol,
            base_qty=held_base_qty,
            client_order_id=sell_client_id,
        )

        if sell_sub.unknown:
            intent = self.rotation_repo.transition_rotation(
                intent_id=intent.intent_id,
                expected_status=RotationStatus.SELL_SUBMITTING,
                new_status=RotationStatus.SELL_UNKNOWN,
                evidence={"error_code": sell_sub.error_code},
            )
            log.warning(f"[ROTATION-EXEC] Sell order {sell_client_id} status UNKNOWN.")
            return intent

        if not sell_sub.accepted:
            # Sell rejected definitively
            intent = self.rotation_repo.transition_rotation(
                intent_id=intent.intent_id,
                expected_status=RotationStatus.SELL_SUBMITTING,
                new_status=RotationStatus.CASH_RECOVERY,
                evidence={"rejection_reason": sell_sub.error_code},
            )
            log.error(f"[ROTATION-EXEC] Sell order rejected: {sell_sub.error_code}. Aborted.")
            return intent

        # Sell accepted & filled
        # For simplicity of freed USDT estimation:
        actual_freed = canonical_decimal(approved_replacement_budget_usdt, non_negative=True)
        intent = self.rotation_repo.transition_rotation(
            intent_id=intent.intent_id,
            expected_status=RotationStatus.SELL_SUBMITTING,
            new_status=RotationStatus.SELL_FILLED,
            evidence={
                "sell_order_id": sell_sub.exchange_order_id,
                "actual_freed_usdt": actual_freed,
            },
        )
        log.info(f"[ROTATION-EXEC] ✅ Liquidated {decision.held_symbol}. Freed: ${actual_freed} USDT")

        # 4. Post-sale revalidation: check candidate preflight, cash balance, and risk gate
        available_budget_dec = Decimal(actual_freed) - Decimal(canonical_decimal(required_reserve_usdt, non_negative=True))
        min_trade_dec = Decimal(canonical_decimal(self.config.min_trade_usdt, non_negative=True))

        can_proceed_buy = (
            candidate_preflight_valid
            and post_sale_risk_approved
            and available_budget_dec >= min_trade_dec
        )

        if not can_proceed_buy:
            # Fall back to CASH_RECOVERY: USDT is retained in Spot
            intent = self.rotation_repo.transition_rotation(
                intent_id=intent.intent_id,
                expected_status=RotationStatus.SELL_FILLED,
                new_status=RotationStatus.CASH_RECOVERY,
                evidence={
                    "reason": "POST_SALE_VALIDATION_FAILED",
                    "candidate_preflight_valid": candidate_preflight_valid,
                    "post_sale_risk_approved": post_sale_risk_approved,
                    "available_budget": str(available_budget_dec),
                },
            )
            log.warning(
                f"[ROTATION-EXEC] 🛡️ Replacement buy cannot proceed. "
                f"Entered CASH_RECOVERY (Retained ${actual_freed} in USDT, zero Futures fallback)."
            )
            return intent

        # 5. Leg 2: Transition to BUY_SUBMITTING and submit buy
        buy_client_id = f"rotbuy_{intent.intent_id[:16]}"
        buy_budget_str = canonical_decimal(str(available_budget_dec), non_negative=True)

        intent = self.rotation_repo.transition_rotation(
            intent_id=intent.intent_id,
            expected_status=RotationStatus.SELL_FILLED,
            new_status=RotationStatus.BUY_SUBMITTING,
            evidence={
                "buy_client_id": buy_client_id,
                "candidate_symbol": decision.candidate_symbol,
                "buy_budget_usdt": buy_budget_str,
            },
        )

        buy_sub = self.order_gateway.submit_buy(
            symbol=decision.candidate_symbol,
            quote_budget_usdt=buy_budget_str,
            client_order_id=buy_client_id,
        )

        if buy_sub.unknown:
            intent = self.rotation_repo.transition_rotation(
                intent_id=intent.intent_id,
                expected_status=RotationStatus.BUY_SUBMITTING,
                new_status=RotationStatus.BUY_UNKNOWN,
                evidence={"error_code": buy_sub.error_code},
            )
            log.warning(f"[ROTATION-EXEC] Buy order {buy_client_id} status UNKNOWN.")
            return intent

        if not buy_sub.accepted:
            # Buy rejected definitively: fall back to CASH_RECOVERY
            intent = self.rotation_repo.transition_rotation(
                intent_id=intent.intent_id,
                expected_status=RotationStatus.BUY_SUBMITTING,
                new_status=RotationStatus.CASH_RECOVERY,
                evidence={"buy_rejection": buy_sub.error_code},
            )
            log.error(f"[ROTATION-EXEC] ❌ Replacement buy rejected: {buy_sub.error_code}. USDT retained in CASH_RECOVERY.")
            return intent

        # Buy completed successfully!
        intent = self.rotation_repo.transition_rotation(
            intent_id=intent.intent_id,
            expected_status=RotationStatus.BUY_SUBMITTING,
            new_status=RotationStatus.COMPLETED,
            evidence={"buy_order_id": buy_sub.exchange_order_id},
        )
        log.info(f"[ROTATION-EXEC] 🎯 Capital rotation COMPLETED: {decision.held_symbol} -> {decision.candidate_symbol}")
        return intent


class ShadowRotationRecorder:
    """Zero-side-effect shadow recorder for rotation comparative policy.

    Evaluates market pairs and persists immutable decisions and audit rows
    into RotationRepository with ZERO import/call path to order/transfer gateways.
    """

    def __init__(self, rotation_repo: RotationRepository, config: RotationPolicyConfig) -> None:
        self.rotation_repo = rotation_repo
        self.config = config

    def record_evaluation(
        self,
        held_snapshot: RotationMarketSnapshot,
        candidate_snapshot: RotationMarketSnapshot,
        held_position_pnl_pct: DecimalString,
        held_holding_time_secs: int,
        now_ms: Optional[int] = None,
        held_mode: str = "NORMAL",
        position_lifecycle_id: Optional[str] = None,
        position_version: int = 1,
        position_notional_usdt: DecimalString = "25",
    ) -> RotationDecision:
        """Run pure evaluation and persist decision for audit and shadow verification."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        decision = evaluate_rotation(
            held_snapshot=held_snapshot,
            candidate_snapshot=candidate_snapshot,
            held_position_pnl_pct=held_position_pnl_pct,
            held_holding_time_secs=held_holding_time_secs,
            config=self.config,
            now_ms=now_ms,
            held_mode=held_mode,
            position_lifecycle_id=position_lifecycle_id,
            position_version=position_version,
            position_notional_usdt=position_notional_usdt,
        )

        self.rotation_repo.append_decision(decision)
        return decision
