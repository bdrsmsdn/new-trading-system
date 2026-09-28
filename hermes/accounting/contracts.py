"""Normative Phase 2 domain and port contracts.

These types define boundaries between accounting repositories, pure policies,
and exchange executors. Monetary values cross persistence and adapter boundaries
as canonical decimal strings; implementations must parse them with Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple


DecimalString = str
UtcDay = str
ReasonCodes = Tuple[str, ...]


class Venue(str, Enum):
    SPOT = "SPOT"
    FUTURES = "FUTURES"


class TradeSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ValuationStatus(str, Enum):
    VALUED = "VALUED"
    PENDING = "PENDING"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class Completeness(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"


class ReconciliationStatus(str, Enum):
    RECONCILED = "RECONCILED"
    PENDING = "PENDING"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class CutoverStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class DistributionAction(str, Enum):
    PLAN_TRANSFER = "PLAN_TRANSFER"
    NO_ACTION = "NO_ACTION"
    BLOCKED = "BLOCKED"


class TransferStatus(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    CONFIRMED = "CONFIRMED"
    FAILED_FINAL = "FAILED_FINAL"
    QUARANTINED = "QUARANTINED"


class TransferSubmissionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DEFINITIVE_REJECTION = "DEFINITIVE_REJECTION"
    UNKNOWN = "UNKNOWN"


class RotationAction(str, Enum):
    APPROVE = "APPROVE"
    NO_ROTATION = "NO_ROTATION"
    BLOCKED = "BLOCKED"


class RotationStatus(str, Enum):
    PLANNED = "PLANNED"
    SELL_SUBMITTING = "SELL_SUBMITTING"
    SELL_UNKNOWN = "SELL_UNKNOWN"
    SELL_FILLED = "SELL_FILLED"
    BUY_SUBMITTING = "BUY_SUBMITTING"
    BUY_UNKNOWN = "BUY_UNKNOWN"
    COMPLETED = "COMPLETED"
    CASH_RECOVERY = "CASH_RECOVERY"
    QUARANTINED = "QUARANTINED"


def canonical_decimal(value: str, *, non_negative: bool = False) -> DecimalString:
    """Validate and canonicalize a non-exponent decimal source string.

    Repository and adapter implementations use this at their boundaries. A
    float is intentionally not accepted, preventing Decimal(float) leakage.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("decimal value must be a non-empty source string")
    if value.startswith("+") or "e" in value.lower() or "," in value:
        raise ValueError("decimal value must use plain base-10 notation")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal value") from exc
    if not number.is_finite():
        raise ValueError("decimal value must be finite")
    if non_negative and number < 0:
        raise ValueError("decimal value must be non-negative")
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


@dataclass(frozen=True)
class FillKey:
    account_id: str
    venue: Venue
    symbol: str
    trade_id: str


@dataclass(frozen=True)
class FillEvent:
    schema_version: int
    account_id: str
    venue: Venue
    symbol: str
    trade_id: str
    order_id: str
    event_time_ms: int
    side: TradeSide
    price: DecimalString
    base_qty: DecimalString
    quote_qty: DecimalString
    commission_asset: str
    commission_qty: DecimalString
    commission_usdt: Optional[DecimalString]
    valuation_status: ValuationStatus
    source_payload_hash: str

    @property
    def key(self) -> FillKey:
        return FillKey(self.account_id, self.venue, self.symbol, self.trade_id)


@dataclass(frozen=True)
class Lot:
    schema_version: int
    lot_id: str
    account_id: str
    venue: Venue
    symbol: str
    acquired_trade_id: str
    opened_at_ms: int
    original_base_qty: DecimalString
    remaining_base_qty: DecimalString
    quote_cost_usdt: DecimalString
    allocated_buy_fee_usdt: Optional[DecimalString]
    completeness: Completeness


@dataclass(frozen=True)
class LotAllocation:
    allocation_id: str
    outcome_id: str
    lot_id: str
    opening_fill_key: FillKey
    closing_fill_key: FillKey
    allocated_base_qty: DecimalString
    allocated_cost_usdt: DecimalString
    allocated_buy_fee_usdt: Optional[DecimalString]
    allocated_sell_fee_usdt: Optional[DecimalString]


@dataclass(frozen=True)
class RealizedOutcome:
    schema_version: int
    outcome_id: str
    account_id: str
    venue: Venue
    symbol: str
    sell_fill_key: FillKey
    sold_base_qty: DecimalString
    gross_proceeds_usdt: DecimalString
    fifo_cost_usdt: DecimalString
    buy_fee_usdt: Optional[DecimalString]
    sell_fee_usdt: Optional[DecimalString]
    net_pnl_usdt: Optional[DecimalString]
    completeness: Completeness
    reporting_day_utc: UtcDay
    incomplete_reason_codes: ReasonCodes
    allocations: Tuple[LotAllocation, ...] = ()


@dataclass(frozen=True)
class AccountingCutover:
    cutover_id: str
    account_id: str
    venue: Venue
    cutover_at_ms: int
    baseline_reference: str
    backfill_from_ms: Optional[int]
    backfill_through_ms: int
    status: CutoverStatus
    approved_at_ms: Optional[int]


@dataclass(frozen=True)
class AccountingSnapshot:
    snapshot_id: str
    ledger_revision: int
    account_id: str
    cutover_id: Optional[str]
    cutover_status: CutoverStatus
    cutover_at_ms: Optional[int]
    cumulative_verified_net_pnl_usdt: DecimalString
    cumulative_confirmed_distributions_usdt: DecimalString
    distribution_surplus_usdt: DecimalString
    completeness: Completeness
    reconciliation_status: ReconciliationStatus
    unresolved_reason_codes: ReasonCodes
    has_submitting_transfer: bool
    has_unknown_transfer: bool
    observed_at_ms: int


@dataclass(frozen=True)
class DistributionPolicyConfig:
    policy_version: str
    enabled: bool
    legacy_auto_sweep_enabled: bool
    target_usdt: DecimalString = "1"
    daily_cap_usdt: DecimalString = "1"
    operational_buffer_usdt: DecimalString = "0"
    reserve_fraction: DecimalString = "0.25"


@dataclass(frozen=True)
class DistributionDecision:
    decision_id: str
    action: DistributionAction
    policy_version: str
    reporting_day_utc: UtcDay
    ledger_snapshot_id: str
    ledger_revision: int
    amount_usdt: Optional[DecimalString]
    distribution_surplus_usdt: DecimalString
    required_reserve_usdt: DecimalString
    reason_codes: ReasonCodes
    decided_at_ms: int


@dataclass(frozen=True)
class TransferIntent:
    schema_version: int
    intent_id: str
    account_id: str
    policy_version: str
    reporting_day_utc: UtcDay
    client_transfer_id: str
    amount_usdt: DecimalString
    policy_snapshot_json: str
    status: TransferStatus
    exchange_tran_id: Optional[str]
    created_at_ms: int
    updated_at_ms: int
    confirmed_at_ms: Optional[int]
    last_error_code: Optional[str]


@dataclass(frozen=True)
class TransferSubmission:
    status: TransferSubmissionStatus
    exchange_tran_id: Optional[str]
    submitted_at_ms: int
    raw_response_hash: Optional[str]
    error_code: Optional[str]


@dataclass(frozen=True)
class TransferHistoryRecord:
    exchange_tran_id: str
    client_transfer_id: Optional[str]
    asset: str
    direction: str
    amount: DecimalString
    occurred_at_ms: int
    status: str


@dataclass(frozen=True)
class RotationMarketSnapshot:
    snapshot_id: str
    account_id: str
    venue: Venue
    symbol: str
    position_lifecycle_id: Optional[str]
    position_version: Optional[int]
    model_version: str
    feature_schema_version: str
    observed_at_ms: int
    expires_at_ms: int
    score: DecimalString
    best_bid_usdt: DecimalString
    best_ask_usdt: DecimalString
    spread_fraction: DecimalString
    available_depth_usdt: DecimalString
    closed_bar_ids: Tuple[str, ...]
    completeness: Completeness


@dataclass(frozen=True)
class RotationDecision:
    schema_version: int
    decision_id: str
    account_id: str
    held_symbol: str
    candidate_symbol: str
    position_lifecycle_id: str
    position_version: int
    model_version: str
    held_snapshot_id: str
    candidate_snapshot_id: str
    held_score: DecimalString
    candidate_score: DecimalString
    score_edge: DecimalString
    estimated_roundtrip_cost_usdt: Optional[DecimalString]
    estimated_cost_fraction: Optional[DecimalString]
    expected_net_benefit_usdt: Optional[DecimalString]
    risk_decision_id: Optional[str]
    action: RotationAction
    reason_codes: ReasonCodes
    decided_at_ms: int


@dataclass(frozen=True)
class RotationIntent:
    schema_version: int
    intent_id: str
    account_id: str
    position_lifecycle_id: str
    decision_id: str
    sell_order_id: Optional[str]
    actual_freed_usdt: Optional[DecimalString]
    buy_order_id: Optional[str]
    approved_replacement_budget_usdt: DecimalString
    status: RotationStatus
    created_at_ms: int
    updated_at_ms: int
    last_error_code: Optional[str]


@dataclass(frozen=True)
class SpotOrderSubmission:
    accepted: bool
    unknown: bool
    client_order_id: str
    exchange_order_id: Optional[str]
    submitted_at_ms: int
    raw_response_hash: Optional[str]
    error_code: Optional[str]


class AccountingRepository(Protocol):
    def ingest_fill(self, fill: FillEvent) -> bool:
        """Insert an unseen fill, return False for an identical duplicate."""
        ...

    def apply_fifo_sell(self, sell_key: FillKey) -> RealizedOutcome:
        """Allocate a sell and persist its outcome in one transaction."""
        ...

    def distribution_snapshot(self, account_id: str, observed_at_ms: int) -> AccountingSnapshot:
        """Return one immutable, revisioned policy snapshot."""
        ...


class TransferIntentRepository(Protocol):
    def claim_daily_intent(
        self, decision: DistributionDecision, client_transfer_id: str
    ) -> TransferIntent:
        """Atomically create or return the unique daily policy intent."""
        ...

    def transition_transfer(
        self,
        intent_id: str,
        expected_status: TransferStatus,
        new_status: TransferStatus,
        evidence: Mapping[str, Any],
    ) -> TransferIntent:
        """Compare-and-set a transfer state and append audit evidence."""
        ...

    def unresolved_transfers(self, account_id: str) -> Sequence[TransferIntent]:
        """Return SUBMITTING and UNKNOWN intents requiring read-back."""
        ...


class RotationRepository(Protocol):
    def append_decision(self, decision: RotationDecision) -> None:
        """Persist an immutable live or shadow decision."""
        ...

    def claim_rotation_intent(self, decision: RotationDecision) -> RotationIntent:
        """Atomically enforce unique decision/lifecycle and attempt limits."""
        ...

    def transition_rotation(
        self,
        intent_id: str,
        expected_status: RotationStatus,
        new_status: RotationStatus,
        evidence: Mapping[str, Any],
    ) -> RotationIntent:
        """Compare-and-set rotation state and append audit evidence."""
        ...


class TransferGateway(Protocol):
    def submit_spot_to_funding(
        self,
        *,
        asset: str,
        amount: DecimalString,
        client_transfer_id: str,
    ) -> TransferSubmission:
        """Submit one MAIN_FUNDING transfer without policy decisions."""
        ...

    def transfer_history(
        self, *, account_id: str, start_ms: int, end_ms: int
    ) -> Sequence[TransferHistoryRecord]:
        """Read universal-transfer history without mutation."""
        ...


class SpotOrderGateway(Protocol):
    def submit_sell(
        self,
        *,
        symbol: str,
        base_qty: DecimalString,
        client_order_id: str,
    ) -> SpotOrderSubmission:
        """Submit one Spot sell; no Futures fallback exists on this port."""
        ...

    def submit_buy(
        self,
        *,
        symbol: str,
        quote_budget_usdt: DecimalString,
        client_order_id: str,
    ) -> SpotOrderSubmission:
        """Submit one Spot buy bounded by the approved quote budget."""
        ...

    def read_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        """Read order state for reconciliation without mutation."""
        ...
