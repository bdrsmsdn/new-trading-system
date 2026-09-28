"""Spot-to-Funding daily profit sweep collector.

Implements pure distribution policy evaluation, exactly-once daily transfer claiming,
Binance transfer gateway integration, and read-only reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import time
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union
import uuid

from hermes.accounting.contracts import (
    AccountingRepository,
    AccountingSnapshot,
    Completeness,
    CutoverStatus,
    DecimalString,
    DistributionAction,
    DistributionDecision,
    DistributionPolicyConfig,
    ReasonCodes,
    ReconciliationStatus,
    TransferGateway,
    TransferHistoryRecord,
    TransferIntent,
    TransferIntentRepository,
    TransferStatus,
    TransferSubmission,
    TransferSubmissionStatus,
    UtcDay,
    canonical_decimal,
)
from hermes.logging_setup import log


def evaluate_distribution(
    policy_config: DistributionPolicyConfig,
    accounting_snapshot: AccountingSnapshot,
    free_spot_usdt: DecimalString,
    total_equity_usdt: DecimalString,
    open_risk_usdt: DecimalString,
    now_ms: int,
) -> DistributionDecision:
    """Pure policy evaluation for Spot-to-Funding daily profit distribution.

    Performs NO I/O. Computes eligibility, portfolio reserve constraints,
    and returns an immutable DistributionDecision with stable reason codes.
    """
    decision_id = f"dec_{uuid.uuid4().hex}"
    reporting_day_utc: UtcDay = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")

    # 1. Validate decimal inputs
    try:
        free_spot_dec = Decimal(canonical_decimal(free_spot_usdt, non_negative=True))
        total_equity_dec = Decimal(canonical_decimal(total_equity_usdt, non_negative=True))
        open_risk_dec = Decimal(canonical_decimal(open_risk_usdt, non_negative=True))
        target_dec = Decimal(canonical_decimal(policy_config.target_usdt, non_negative=True))
        buffer_dec = Decimal(canonical_decimal(policy_config.operational_buffer_usdt, non_negative=True))
        reserve_fraction_dec = Decimal(canonical_decimal(policy_config.reserve_fraction, non_negative=True))
        surplus_dec = Decimal(canonical_decimal(accounting_snapshot.distribution_surplus_usdt))
    except (ValueError, InvalidOperation):
        return DistributionDecision(
            decision_id=decision_id,
            action=DistributionAction.BLOCKED,
            policy_version=policy_config.policy_version,
            reporting_day_utc=reporting_day_utc,
            ledger_snapshot_id=accounting_snapshot.snapshot_id,
            ledger_revision=accounting_snapshot.ledger_revision,
            amount_usdt=None,
            distribution_surplus_usdt=accounting_snapshot.distribution_surplus_usdt,
            required_reserve_usdt="0",
            reason_codes=("INVALID_INPUT",),
            decided_at_ms=now_ms,
        )

    # Required portfolio reserve calculation:
    # max(operational_buffer_usdt, total_equity_usdt * reserve_fraction + open_risk_usdt)
    fractional_risk_reserve = (total_equity_dec * reserve_fraction_dec) + open_risk_dec
    required_reserve_dec = max(buffer_dec, fractional_risk_reserve)
    required_reserve_str = canonical_decimal(str(required_reserve_dec), non_negative=True)

    reasons: List[str] = []

    # 2. Gate 1: Feature flags
    if not policy_config.enabled or policy_config.legacy_auto_sweep_enabled:
        reasons.append("FEATURE_DISABLED")

    # 3. Gate 2: Cutover status
    if accounting_snapshot.cutover_status != CutoverStatus.APPROVED:
        reasons.append("CUTOVER_NOT_APPROVED")

    # 4. Gate 3: Accounting completeness & reconciliation
    if accounting_snapshot.completeness != Completeness.VERIFIED:
        reasons.append("ACCOUNTING_INCOMPLETE")
    if accounting_snapshot.reconciliation_status != ReconciliationStatus.RECONCILED:
        reasons.append("RECONCILIATION_REQUIRED")
    if accounting_snapshot.unresolved_reason_codes:
        for code in accounting_snapshot.unresolved_reason_codes:
            if code not in reasons:
                reasons.append(code)

    # 5. Gate 4: Active / Unknown transfer reconciliation
    if accounting_snapshot.has_submitting_transfer or accounting_snapshot.has_unknown_transfer:
        reasons.append("UNKNOWN_TRANSFER")

    # If blocked by integrity/config gates, fail closed immediately
    if reasons:
        return DistributionDecision(
            decision_id=decision_id,
            action=DistributionAction.BLOCKED,
            policy_version=policy_config.policy_version,
            reporting_day_utc=reporting_day_utc,
            ledger_snapshot_id=accounting_snapshot.snapshot_id,
            ledger_revision=accounting_snapshot.ledger_revision,
            amount_usdt=None,
            distribution_surplus_usdt=accounting_snapshot.distribution_surplus_usdt,
            required_reserve_usdt=required_reserve_str,
            reason_codes=tuple(reasons),
            decided_at_ms=now_ms,
        )

    # 6. Gate 5: Cumulative verified surplus threshold (>= 1.00 USDT)
    if surplus_dec < target_dec:
        return DistributionDecision(
            decision_id=decision_id,
            action=DistributionAction.NO_ACTION,
            policy_version=policy_config.policy_version,
            reporting_day_utc=reporting_day_utc,
            ledger_snapshot_id=accounting_snapshot.snapshot_id,
            ledger_revision=accounting_snapshot.ledger_revision,
            amount_usdt=None,
            distribution_surplus_usdt=accounting_snapshot.distribution_surplus_usdt,
            required_reserve_usdt=required_reserve_str,
            reason_codes=("SURPLUS_BELOW_TARGET",),
            decided_at_ms=now_ms,
        )

    # 7. Gate 6: Free spot balance availability
    if free_spot_dec < (target_dec + buffer_dec):
        return DistributionDecision(
            decision_id=decision_id,
            action=DistributionAction.BLOCKED,
            policy_version=policy_config.policy_version,
            reporting_day_utc=reporting_day_utc,
            ledger_snapshot_id=accounting_snapshot.snapshot_id,
            ledger_revision=accounting_snapshot.ledger_revision,
            amount_usdt=None,
            distribution_surplus_usdt=accounting_snapshot.distribution_surplus_usdt,
            required_reserve_usdt=required_reserve_str,
            reason_codes=("INSUFFICIENT_FREE_USDT",),
            decided_at_ms=now_ms,
        )

    # 8. Gate 7: Post-transfer portfolio reserve compliance
    post_transfer_reserve = free_spot_dec - target_dec
    if post_transfer_reserve < required_reserve_dec:
        return DistributionDecision(
            decision_id=decision_id,
            action=DistributionAction.BLOCKED,
            policy_version=policy_config.policy_version,
            reporting_day_utc=reporting_day_utc,
            ledger_snapshot_id=accounting_snapshot.snapshot_id,
            ledger_revision=accounting_snapshot.ledger_revision,
            amount_usdt=None,
            distribution_surplus_usdt=accounting_snapshot.distribution_surplus_usdt,
            required_reserve_usdt=required_reserve_str,
            reason_codes=("RESERVE_BREACH",),
            decided_at_ms=now_ms,
        )

    # All gates passed: PLAN_TRANSFER for exactly target_usdt (1.00 USDT)
    return DistributionDecision(
        decision_id=decision_id,
        action=DistributionAction.PLAN_TRANSFER,
        policy_version=policy_config.policy_version,
        reporting_day_utc=reporting_day_utc,
        ledger_snapshot_id=accounting_snapshot.snapshot_id,
        ledger_revision=accounting_snapshot.ledger_revision,
        amount_usdt=canonical_decimal(policy_config.target_usdt, non_negative=True),
        distribution_surplus_usdt=accounting_snapshot.distribution_surplus_usdt,
        required_reserve_usdt=required_reserve_str,
        reason_codes=(),
        decided_at_ms=now_ms,
    )


class BinanceTransferGateway:
    """Production and adapter implementation of TransferGateway protocol.

    Translates domain transfer calls into authenticated Binance API endpoints,
    mapping timeouts and errors into explicit TransferSubmission states.
    """

    def submit_spot_to_funding(
        self,
        *,
        asset: str,
        amount: DecimalString,
        client_transfer_id: str,
    ) -> TransferSubmission:
        """Submit one MAIN_FUNDING transfer via Binance Universal Transfer endpoint."""
        now_ms = int(time.time() * 1000)
        canonical_amt = canonical_decimal(amount, non_negative=True)
        try:
            from hermes.api.auth import binance_signed_request
            params = {
                "type": "MAIN_FUNDING",
                "asset": asset.upper(),
                "amount": canonical_amt,
                "clientTranId": client_transfer_id,
            }
            res = binance_signed_request("/sapi/v1/asset/transfer", params=params, method="POST")
            if isinstance(res, dict) and "tranId" in res:
                return TransferSubmission(
                    status=TransferSubmissionStatus.ACCEPTED,
                    exchange_tran_id=str(res["tranId"]),
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code=None,
                )
            elif isinstance(res, dict) and res.get("code", 0) < 0:
                # Binance error codes
                err_code = str(res.get("code"))
                msg = str(res.get("msg", ""))
                # Definitive rejections: invalid asset, insufficient balance, etc.
                if err_code in ("-1000", "-1013", "-1100", "-1102", "-2010"):
                    return TransferSubmission(
                        status=TransferSubmissionStatus.DEFINITIVE_REJECTION,
                        exchange_tran_id=None,
                        submitted_at_ms=now_ms,
                        raw_response_hash=None,
                        error_code=f"{err_code}:{msg}",
                    )
                return TransferSubmission(
                    status=TransferSubmissionStatus.UNKNOWN,
                    exchange_tran_id=None,
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code=f"{err_code}:{msg}",
                )
            else:
                return TransferSubmission(
                    status=TransferSubmissionStatus.UNKNOWN,
                    exchange_tran_id=None,
                    submitted_at_ms=now_ms,
                    raw_response_hash=None,
                    error_code="UNEXPECTED_RESPONSE",
                )
        except Exception as exc:
            log.error(f"[TRANSFER-GATEWAY] Exception submitting transfer {client_transfer_id}: {exc}")
            return TransferSubmission(
                status=TransferSubmissionStatus.UNKNOWN,
                exchange_tran_id=None,
                submitted_at_ms=now_ms,
                raw_response_hash=None,
                error_code=type(exc).__name__,
            )

    def transfer_history(
        self, *, account_id: str, start_ms: int, end_ms: int
    ) -> Sequence[TransferHistoryRecord]:
        """Read universal-transfer history for reconciliation."""
        try:
            from hermes.api.auth import binance_signed_request
            params = {
                "type": "MAIN_FUNDING",
                "startTime": start_ms,
                "endTime": end_ms,
                "size": 100,
            }
            res = binance_signed_request("/sapi/v1/asset/transfer", params=params, method="GET")
            records: List[TransferHistoryRecord] = []
            if isinstance(res, dict) and "rows" in res:
                for row in res["rows"]:
                    records.append(
                        TransferHistoryRecord(
                            exchange_tran_id=str(row.get("tranId", "")),
                            client_transfer_id=row.get("clientTranId"),
                            asset=str(row.get("asset", "")),
                            direction=str(row.get("type", "")),
                            amount=canonical_decimal(str(row.get("amount", "0")), non_negative=True),
                            occurred_at_ms=int(row.get("timestamp", 0)),
                            status=str(row.get("status", "")),
                        )
                    )
            return records
        except Exception as exc:
            log.error(f"[TRANSFER-GATEWAY] Error reading transfer history: {exc}")
            return ()


class SpotToFundingCollector:
    """Orchestrates exactly-once daily Spot-to-Funding profit sweep transfers."""

    def __init__(
        self,
        accounting_repo: AccountingRepository,
        intent_repo: TransferIntentRepository,
        transfer_gateway: TransferGateway,
        policy_config: DistributionPolicyConfig,
    ) -> None:
        self.accounting_repo = accounting_repo
        self.intent_repo = intent_repo
        self.transfer_gateway = transfer_gateway
        self.policy_config = policy_config

    def reconcile_unresolved_transfers(
        self, account_id: str = "default", now_ms: Optional[int] = None
    ) -> Sequence[TransferIntent]:
        """Reconcile SUBMITTING and UNKNOWN intents against read-only transfer history."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        unresolved = self.intent_repo.unresolved_transfers(account_id)
        reconciled: List[TransferIntent] = []

        for intent in unresolved:
            start_window = max(0, intent.created_at_ms - (300 * 1000))
            end_window = now_ms + (60 * 1000)

            history = self.transfer_gateway.transfer_history(
                account_id=account_id,
                start_ms=start_window,
                end_ms=end_window,
            )

            # Match records:
            # 1. By client_transfer_id if available
            # 2. Or by direction MAIN_FUNDING, asset USDT, exact Decimal amount, and timestamp proximity
            matches = [
                h for h in history
                if (h.client_transfer_id and h.client_transfer_id == intent.client_transfer_id)
                or (
                    h.asset == "USDT"
                    and h.direction == "MAIN_FUNDING"
                    and Decimal(h.amount) == Decimal(intent.amount_usdt)
                    and abs(h.occurred_at_ms - intent.created_at_ms) <= 120_000
                )
            ]

            if len(matches) == 1:
                match = matches[0]
                updated = self.intent_repo.transition_transfer(
                    intent_id=intent.intent_id,
                    expected_status=intent.status,
                    new_status=TransferStatus.CONFIRMED,
                    evidence={
                        "reconciled_by": "history_match",
                        "exchange_tran_id": match.exchange_tran_id,
                        "occurred_at_ms": match.occurred_at_ms,
                    },
                )
                reconciled.append(updated)
                log.info(f"[COLLECTOR] Reconciled intent {intent.intent_id} -> CONFIRMED (tranId: {match.exchange_tran_id})")
            elif len(matches) > 1:
                # Ambiguous multiple matches! Quarantine intent
                updated = self.intent_repo.transition_transfer(
                    intent_id=intent.intent_id,
                    expected_status=intent.status,
                    new_status=TransferStatus.QUARANTINED,
                    evidence={
                        "quarantine_reason": "AMBIGUOUS_HISTORY_MATCHES",
                        "match_count": len(matches),
                    },
                )
                reconciled.append(updated)
                log.error(f"[COLLECTOR] Ambiguous matches for intent {intent.intent_id}! QUARANTINED.")
            else:
                # No matches found yet
                # If intent has been UNKNOWN for more than reconciliation window (e.g. 10m), mark FAILED_FINAL
                age_ms = now_ms - intent.created_at_ms
                if intent.status == TransferStatus.UNKNOWN and age_ms > (600 * 1000):
                    updated = self.intent_repo.transition_transfer(
                        intent_id=intent.intent_id,
                        expected_status=TransferStatus.UNKNOWN,
                        new_status=TransferStatus.FAILED_FINAL,
                        evidence={
                            "reconciled_by": "authoritative_history_absence",
                            "checked_window_ms": age_ms,
                        },
                    )
                    reconciled.append(updated)
                    log.info(f"[COLLECTOR] Intent {intent.intent_id} absent from history -> FAILED_FINAL")

        return reconciled

    def run_daily_collection_tick(
        self,
        account_id: str = "default",
        free_spot_usdt: DecimalString = "0",
        total_equity_usdt: DecimalString = "0",
        open_risk_usdt: DecimalString = "0",
        now_ms: Optional[int] = None,
    ) -> Tuple[DistributionDecision, Optional[TransferIntent]]:
        """Run one evaluation tick of the daily profit sweep collector."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        # 1. Reconcile any in-flight / unknown transfers first
        self.reconcile_unresolved_transfers(account_id=account_id, now_ms=now_ms)

        # 2. Obtain immutable snapshot from accounting ledger
        snapshot = self.accounting_repo.distribution_snapshot(account_id=account_id, observed_at_ms=now_ms)

        # 3. Evaluate pure policy
        decision = evaluate_distribution(
            policy_config=self.policy_config,
            accounting_snapshot=snapshot,
            free_spot_usdt=free_spot_usdt,
            total_equity_usdt=total_equity_usdt,
            open_risk_usdt=open_risk_usdt,
            now_ms=now_ms,
        )

        if decision.action != DistributionAction.PLAN_TRANSFER:
            return decision, None

        # 4. Generate client transfer ID and atomically claim daily intent
        client_transfer_id = f"tf_{decision.reporting_day_utc.replace('-', '')}_{uuid.uuid4().hex[:12]}"
        try:
            intent = self.intent_repo.claim_daily_intent(
                decision=decision,
                client_transfer_id=client_transfer_id,
            )
        except Exception as exc:
            log.warning(f"[COLLECTOR] Claim daily intent conflict or error: {exc}")
            return decision, None

        # If the claimed intent is not PLANNED (e.g. already existed in SUBMITTING/CONFIRMED), do not resend
        if intent.status != TransferStatus.PLANNED:
            log.info(f"[COLLECTOR] Intent already claimed with status {intent.status.value}")
            return decision, None

        # 5. Transition to SUBMITTING before calling external gateway
        intent = self.intent_repo.transition_transfer(
            intent_id=intent.intent_id,
            expected_status=TransferStatus.PLANNED,
            new_status=TransferStatus.SUBMITTING,
            evidence={
                "client_transfer_id": client_transfer_id,
                "amount_usdt": decision.amount_usdt,
                "submitted_at_ms": now_ms,
            },
        )

        # 6. Execute external transfer via gateway
        amount_to_send = decision.amount_usdt or "1"
        sub = self.transfer_gateway.submit_spot_to_funding(
            asset="USDT",
            amount=amount_to_send,
            client_transfer_id=client_transfer_id,
        )

        # 7. Process submission response
        if sub.status == TransferSubmissionStatus.ACCEPTED:
            intent = self.intent_repo.transition_transfer(
                intent_id=intent.intent_id,
                expected_status=TransferStatus.SUBMITTING,
                new_status=TransferStatus.CONFIRMED,
                evidence={
                    "exchange_tran_id": sub.exchange_tran_id,
                    "submitted_at_ms": sub.submitted_at_ms,
                },
            )
            log.info(f"[COLLECTOR] ✅ Daily profit transfer CONFIRMED (tranId: {sub.exchange_tran_id})")
        elif sub.status == TransferSubmissionStatus.DEFINITIVE_REJECTION:
            intent = self.intent_repo.transition_transfer(
                intent_id=intent.intent_id,
                expected_status=TransferStatus.SUBMITTING,
                new_status=TransferStatus.FAILED_FINAL,
                evidence={
                    "error_code": sub.error_code,
                    "submitted_at_ms": sub.submitted_at_ms,
                },
            )
            log.warning(f"[COLLECTOR] ❌ Transfer rejected definitively: {sub.error_code}")
        else:
            # UNKNOWN response / timeout
            intent = self.intent_repo.transition_transfer(
                intent_id=intent.intent_id,
                expected_status=TransferStatus.SUBMITTING,
                new_status=TransferStatus.UNKNOWN,
                evidence={
                    "error_code": sub.error_code,
                    "submitted_at_ms": sub.submitted_at_ms,
                },
            )
            log.warning(f"[COLLECTOR] ⚠️ Transfer response UNKNOWN. Marked for reconciliation.")

        return decision, intent
