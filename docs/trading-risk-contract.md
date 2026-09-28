# Trading Risk Contract

Status: normative architecture contract for the safety-remediation release

Baseline: `d779dc62e3cecd647c48d4ab76bd57c4b1d51d6f`

Scope: Spot and Futures decision data, accounting events, policy precedence, checkpoint lifecycle, and fail-closed behavior

## 1. Normative language and invariants

The words MUST, MUST NOT, SHOULD, and MAY are normative.

1. Safety decisions are deterministic at the execution boundary. AI, news, indicators, and strategy labels are advisory evidence and MUST NOT veto, postpone, or weaken a triggered protective exit.
2. Unknown, missing, stale, non-finite, inconsistent, or unvalued input is `UNKNOWN`; it MUST NOT be replaced with zero, a neutral score, a cached default, or a fabricated price.
3. Monetary values and quantities MUST use `Decimal`, serialized as base-10 strings. Binary floating point MUST NOT be used for accounting, exchange filters, or stop calculations.
4. All domain timestamps are timezone-aware UTC instants. Serialized timestamps use RFC 3339 with a `Z` suffix. A timestamp without an offset is invalid.
5. Symbols MUST be canonicalized at the exchange-adapter boundary. Every persistent identifier is scoped by account and venue; a bare `symbol` or `trade_id` is not globally unique.
6. A decision is valid only for the immutable snapshot and position lifecycle/version that produced it. A decision MUST NOT be applied after its deadline or after quantity, side, account, venue, position lifecycle, or active exit intent changes.
7. A stop or protected floor may only become more protective: for a LONG, the trigger price can only increase; for a SHORT, it can only decrease. A discretionary result MUST NOT loosen it.
8. An uncertain external order or transfer result remains pending reconciliation. It MUST NOT be blindly retried.
9. New-risk controls and protective-exit controls are independent. Disabling entries, rotations, additions, or collections MUST NOT disable reconciliation, cancellation, or exits.
10. The existing gross +10% checkpoint and current safety thresholds remain baseline behavior for this remediation. Threshold optimization is outside this contract.

## 2. Common types

The following Python-like definitions are language-neutral contracts. Implementations may use frozen dataclasses or equivalent immutable value objects.

```python
DecimalString = str       # regex: ^-?(0|[1-9][0-9]*)(\.[0-9]+)?$
UtcTimestamp = str        # RFC 3339 UTC, e.g. 2026-09-28T12:34:56.123Z
SnapshotId = str          # stable digest/UUID for immutable canonical payload
AccountId = str
CanonicalSymbol = str     # e.g. BTCUSDT after adapter canonicalization

Venue = Literal["SPOT", "FUTURES"]
PositionSide = Literal["LONG", "SHORT"]
TradeSide = Literal["BUY", "SELL"]
DataStatus = Literal["FRESH", "STALE", "UNKNOWN", "INVALID"]
DataSource = Literal["EXCHANGE", "CACHE", "DERIVED", "CROSS_MARKET_PROXY"]
ValuationStatus = Literal["VALUED", "PENDING", "UNAVAILABLE", "INVALID"]
Completeness = Literal["COMPLETE", "INCOMPLETE", "UNKNOWN"]
```

Every quantity declares its unit in its field name or containing type. Percentage fractions use decimal fractions (`0.025` means 2.5%). Futures ROE drawdowns use percentage points (`2.5` means 2.5 ROE points). These units are not interchangeable.

## 3. MomentumSnapshot

`MomentumSnapshot` is immutable and self-contained. It represents exactly one coherent continuation or signal-policy observation; consumers MUST NOT silently supplement it with later cache reads.

```python
@dataclass(frozen=True)
class Provenance:
    source: DataSource
    source_id: str | None             # request/candle/cache-entry identifier
    observed_at_utc: UtcTimestamp | None
    received_at_utc: UtcTimestamp | None
    max_age_ms: int
    age_ms_at_snapshot: int | None
    status: DataStatus
    reason_code: str | None

@dataclass(frozen=True)
class TimeframeSeries:
    timeframe: str                    # canonical exchange interval, e.g. "1h"
    closed_bar_open_times_utc: tuple[UtcTimestamp, ...]
    values: tuple[DecimalString, ...] | None
    provenance: Provenance

@dataclass(frozen=True)
class MomentumSnapshot:
    snapshot_id: SnapshotId
    schema_version: int
    symbol: CanonicalSymbol
    venue: Venue
    side: PositionSide
    position_lifecycle_id: str
    position_version: int
    snapshot_started_at_utc: UtcTimestamp
    snapshot_completed_at_utc: UtcTimestamp
    best_bid_price: DecimalString | None
    best_ask_price: DecimalString | None
    bid_base_qty: DecimalString | None
    ask_base_qty: DecimalString | None
    bid_quote_notional: DecimalString | None
    ask_quote_notional: DecimalString | None
    spread_quote: DecimalString | None
    spread_fraction: DecimalString | None
    orderbook_provenance: Provenance
    timeframe_series: tuple[TimeframeSeries, ...]
    cross_market_proxy: bool
    proxy_venue: Venue | None
```

Validation rules:

- `snapshot_id` MUST identify the canonical immutable payload, excluding no decision-relevant fields.
- `snapshot_started_at_utc <= snapshot_completed_at_utc`.
- A book is continuation-eligible only when both sides exist, prices and quantities are finite and strictly positive, `best_bid_price < best_ask_price`, provenance is `FRESH`, and age is at most `max_age_ms`.
- Base quantities are asset units, not dollars. Quote notionals MUST be explicitly calculated from price and base quantity before being labelled as USDT or another quote asset.
- `spread_quote = best_ask_price - best_bid_price`; `spread_fraction` MUST document and consistently use a denominator (normatively, midpoint). A negative or zero spread is invalid.
- Each required timeframe series MUST contain distinct closed bars in chronological order and satisfy its own TTL. Re-reading one cached bar does not count as multiple confirmations.
- `values=None` plus an explanatory provenance status represents absence. Neutral placeholders such as RSI 50 or imbalance 1.0 are forbidden for missing data.
- Futures evaluation MUST use Futures depth. A Spot observation MAY be supplied only as `CROSS_MARKET_PROXY`, with `cross_market_proxy=true` and `proxy_venue="SPOT"`; it cannot silently satisfy a required Futures-depth gate.
- LONG and SHORT are explicit. Directional evidence MUST be interpreted relative to `side`; bullish evidence cannot be reused unchanged as SHORT-continuation evidence.
- Any required field with `STALE`, `UNKNOWN`, or `INVALID` status makes the snapshot ineligible to authorize a new continuation, entry, addition, or rotation. It does not suppress an existing protective exit.

## 4. FillEvent and realized accounting

### 4.1 FillEvent

A `FillEvent` is one exchange-confirmed trade fill, not an order request or aggregate quote.

```python
@dataclass(frozen=True)
class FillEvent:
    schema_version: int
    account: AccountId
    venue: Venue
    symbol: CanonicalSymbol
    trade_id: str
    order_id: str
    event_time_utc: UtcTimestamp
    side: TradeSide
    base_qty: DecimalString
    quote_qty: DecimalString
    commission_asset: str
    commission_qty: DecimalString
    commission_usdt: DecimalString | None
    valuation_status: ValuationStatus
```

Rules:

- Idempotency key is `(account, venue, symbol, trade_id)`. Duplicate ingestion MUST be a no-op after verifying that the payload is identical; conflicting payloads are quarantined.
- `base_qty`, `quote_qty`, and `commission_qty` are non-negative, finite decimals. Confirmed fills require positive executed `base_qty` and `quote_qty`.
- `quote_qty` is actual exchange-reported executed quote quantity, not requested quantity times a supplied price.
- `commission_usdt` is required only when `valuation_status="VALUED"`. It is `None` otherwise; zero is valid only when the exchange explicitly reports no commission.
- Third-asset commission valuation uses the documented execution-time valuation source. Pending/unavailable valuation keeps affected outcomes incomplete and blocks profit collection.
- Pagination and replay MUST preserve every event, including losses and fills occurring after a daily collection target was met.

### 4.2 LotAllocation and RealizedOutcome

```python
@dataclass(frozen=True)
class LotAllocation:
    opening_fill_key: tuple[AccountId, Venue, CanonicalSymbol, str]
    closing_fill_key: tuple[AccountId, Venue, CanonicalSymbol, str]
    allocated_base_qty: DecimalString
    allocated_cost: DecimalString
    allocated_entry_fees: DecimalString | None
    allocated_exit_fees: DecimalString | None

@dataclass(frozen=True)
class RealizedOutcome:
    schema_version: int
    account: AccountId
    venue: Venue
    symbol: CanonicalSymbol
    lot_allocations: tuple[LotAllocation, ...]
    proceeds: DecimalString
    allocated_cost: DecimalString
    all_fees: DecimalString | None
    net_pnl: DecimalString | None
    completeness: Completeness
    incomplete_reason_codes: tuple[str, ...]
```

Rules:

- Spot lot allocation is FIFO unless a separately versioned and approved accounting policy says otherwise.
- `proceeds` is actual closing-fill quote proceeds. `allocated_cost` includes the exact consumed opening-lot cost basis.
- `all_fees` includes allocated entry fees and closing fees exactly once, including third-asset fees after valuation.
- When complete, `net_pnl = proceeds - allocated_cost - all_fees` and `completeness="COMPLETE"`.
- If any opening lot, fill, commission, conversion, or allocation is unresolved, `all_fees` and/or `net_pnl` MUST be `None`, `completeness="INCOMPLETE"`, and reasons MUST be recorded. The implementation MUST NOT coerce missing components to zero.
- Partial exits reduce remaining inventory; they do not delete the position. Futures funding, fees, and realized PnL remain separately attributed and MUST NOT be assumed to be available Spot cash.

## 5. RiskDecision

```python
RiskReasonCode = Literal[
    "ALLOW",
    "STALE_SNAPSHOT",
    "UNKNOWN_DATA",
    "INVALID_DATA",
    "POSITION_CHANGED",
    "PENDING_ORDER",
    "RECONCILIATION_REQUIRED",
    "ENTRY_DISABLED",
    "FUTURES_ENTRY_DISABLED",
    "RISK_BUDGET_EXCEEDED",
    "INSUFFICIENT_RESERVE",
    "MIN_NOTIONAL_EXCEEDS_BUDGET",
    "CIRCUIT_BREAKER_ACTIVE",
    "INVALID_STRATEGY_SETUP",
]

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason_code: RiskReasonCode
    snapshot_id: SnapshotId
```

Rules:

- `allowed=true` is valid only with `reason_code="ALLOW"` and a fresh immutable `snapshot_id`.
- Unknown reason codes fail closed. `allowed=false` never blocks an already-required risk-reducing exit.
- Every order-capable entry/addition path (normal, spike, rotation, DCA, agent/manual tool, and any explicitly enabled Futures strategy) MUST consume the same risk gate contract immediately before capital reservation/order submission.
- A strategy label, confidence value, or caller identity cannot bypass a denied decision.

## 6. Single policy precedence

The execution boundary MUST evaluate these tiers in order. A lower tier cannot override a result from a higher tier.

| Priority | Policy tier | Required behavior |
|---:|---|---|
| 1 | Exchange reconciliation and known pending orders | Reconcile position quantity/version and uncertain, open, partial, or pending intents. Do not submit a duplicate action. Protective management remains active for the confirmed remainder. |
| 2 | Hard risk, hard SL, active trailing, protected-floor breach | Create or maintain one deterministic protective exit intent immediately. Do not call or wait for AI/news/confirmation. BUY/STRONG_BUY cannot cancel it. |
| 3 | Confirmed emergency or thesis invalidation | With fresh deterministic evidence, request a risk-reducing exit even below ordinary profit thresholds. A raw signal label or untrusted headline alone is insufficient. |
| 4 | +10% gross checkpoint | Atomically freeze a coherent snapshot, retain or tighten active protection, transition to `TP_EVALUATING`, and evaluate continuation asynchronously. |
| 5 | Normal momentum weakening | Apply a versioned, strategy-specific confirmed early-exit policy. Overbought/oversold or STRONG_SELL alone is evidence, not an order. |
| 6 | Entry or addition | Require a valid setup, fresh data, shared risk approval, serialized budget reservation, and no conflicting exit/reconciliation state. Existing-position STRONG_BUY does not imply pyramiding. |

Additional invariants:

- Exit intent creation and exchange submission are separate durable operations. Failed or uncertain submission remains `EXIT_PENDING` until read-back establishes the exchange state.
- Before applying any asynchronous result, re-read lifecycle identity, version, side, remaining quantity, pending intent, active stop, and quote freshness.
- A stale quantity MUST NOT be submitted. A late result cannot resurrect a closed position, reverse an exit, widen protection, or start a new evaluation.

## 7. Checkpoint state machine

### 7.1 States

```text
STANDARD -> TRAILING_ARMED -> TP_EVALUATING -> RIDING
    |              |                |             |
    +--------------+----------------+-------------+-> EXIT_PENDING -> CLOSED
```

`TRAILING_ARMED` represents persistent active protection after its activation threshold. An implementation MAY model arming as an orthogonal persisted flag, but its observable transitions and guarantees MUST match this state machine.

State is keyed by `(account, venue, symbol, side, position_lifecycle_id)` and includes a monotonically increasing `position_version`. A close/reopen MUST receive a new lifecycle ID. Hedge-mode sides MUST remain distinct.

### 7.2 Transition contract

| From | Event/guard | To | Atomic actions |
|---|---|---|---|
| `STANDARD` | Trailing activation reached on fresh quote | `TRAILING_ARMED` | Persist first observed peak and armed protection before returning. |
| `STANDARD` or `TRAILING_ARMED` | +10% gross checkpoint reached and no higher-priority exit/pending intent | `TP_EVALUATING` | Freeze snapshot/version, persist evaluation ID/deadline, retain or tighten protection, enqueue async evaluation. |
| `TP_EVALUATING` | Valid `EXTEND` before deadline; current state/version/quote revalidated | `RIDING` | Persist floor/trailing parameters without loosening protection. |
| `TP_EVALUATING` | `TAKE_PROFIT`, missing/invalid/stale required data, evaluator error, or deadline expiry | `EXIT_PENDING` | Persist deterministic take-profit exit intent. |
| Any non-closed state | Hard stop, armed trail, protected floor, or confirmed emergency triggers | `EXIT_PENDING` | Cancel/ignore evaluation and persist protective exit intent immediately. |
| `RIDING` | Ratchet condition on fresh quote | `RIDING` | Persist monotonic peak and tighter protection. |
| `EXIT_PENDING` | Exchange confirms partial fill | `EXIT_PENDING` | Account for fill; update remaining quantity/version; reconcile before any resubmission. |
| `EXIT_PENDING` | Exchange confirms complete close and accounting captured | `CLOSED` | Finalize lifecycle; reject all late evaluations. |
| `EXIT_PENDING` | Timeout/ambiguous/failure | `EXIT_PENDING` | Reconcile by exchange read-back; never return to holding/evaluation blindly. |

### 7.3 Deadline and recovery

- Default total evaluation deadline is 3,000 ms from persisted transition into `TP_EVALUATING`. It includes data fetch, retries, queueing, and optional model work; it is not merely an HTTP timeout.
- On deadline expiry, continuation fails closed: transition to `EXIT_PENDING` for deterministic take profit while the protective loop remains operational.
- During `TP_EVALUATING`, every protective tick continues independently. No evaluator lock, network call, SDK retry, or work on another symbol may block it.
- Results arriving after the deadline or after any state/version change are discarded and audit-logged.
- Crash recovery in `TP_EVALUATING`: if the persisted deadline has expired or a coherent valid result was not durably committed, fail closed to `EXIT_PENDING`. Do not restart the deadline.
- Crash recovery in `EXIT_PENDING`: reconcile order and fills first. Never blindly submit another order.
- Optional periodic thesis reviews in `RIDING` are disabled by default. If later approved, they use distinct fresh closed-bar snapshots and may only tighten protection or request an earlier exit.

## 8. Spot and Futures unit separation

The following names and semantics are mandatory:

```python
spot_price_trail_fraction: DecimalString       # e.g. "0.025" = 2.5% price pullback
futures_roe_drawdown_points: DecimalString     # e.g. "2.5" = 2.5 ROE percentage points
floor_trigger_price: DecimalString | None
floor_trigger_roe: DecimalString | None
```

- Spot trail calculations use instrument price and `spot_price_trail_fraction`.
- Futures trail calculations use explicitly defined ROE and `futures_roe_drawdown_points`, with documented margin/notional/leverage basis. ROE is not net PnL and MUST NOT be presented as net of fees or funding.
- No generic `trail_pct` may cross this boundary. Adapters MUST reject unit-mismatched payloads rather than convert implicitly.
- Futures peak/floor state MUST persist the initial observation immediately and update monotonically. It is scoped to account, venue, symbol, side, and lifecycle; closing one position MUST prune only that lifecycle.
- Alerts MUST visibly distinguish Spot price percent, Futures ROE points, gross checkpoint PnL, and net realized PnL. Trigger values and actual fills are separate facts; no alert may describe a software threshold as guaranteed profit.

## 9. Fail-closed boundaries

### 9.1 Entry and venue behavior

- Automatic Futures fallback after Spot shortage, failed Spot rotation, minimum-notional rejection, or unavailable Spot balance is prohibited.
- New Futures entries are disabled by default (`futures_new_entries_enabled=false`). Enabling requires an explicit approved configuration migration and independent Futures exposure/margin gates; it MUST NOT be inferred from leverage settings or existing positions.
- Existing Futures positions retain reconciliation, stop, trailing, floor, reduce-only exit, and alert processing while new Futures entries are disabled.
- If a route cannot prove whether an action increases or reduces risk, it is denied and reconciled.

### 9.2 Data and decision failures

| Failure | New risk / continuation | Existing protection |
|---|---|---|
| Missing/stale/invalid market or balance data | Deny | Continue from valid persisted protection; fresh quote required for new calculated trigger, but known exchange-native protection is not cancelled. |
| Unknown cost basis/fee valuation | Deny collection and risk-increasing action | Reconcile; risk-reducing exit remains permitted and outcome stays incomplete. |
| Evaluator/model timeout or invalid schema | At checkpoint, do not extend; create take-profit exit intent | Protective loop continues. |
| News unavailable or untrusted | Cannot authorize/veto action alone | Hard exits continue; deterministic emergency requires independent fresh evidence. |
| Pending/uncertain order | No duplicate or conflicting order | Reconcile; manage confirmed remaining exposure. |
| Risk service unavailable/unknown reason | Deny entry/addition/rotation | Exit/cancel/reconciliation remain available. |

## 10. Supporting durable contracts

These supporting records complete boundaries required by downstream ledger and rotation work.

```python
TransferStatus = Literal["INTENDED", "SUBMITTED", "CONFIRMED", "AMBIGUOUS", "FAILED"]

@dataclass(frozen=True)
class TransferIntent:
    id: str
    reporting_day_utc: str            # YYYY-MM-DD; UTC retained for this release
    amount_usdt: DecimalString
    status: TransferStatus
    exchange_tran_id: str | None

@dataclass(frozen=True)
class RotationDecision:
    allowed: bool
    held_snapshot_id: SnapshotId
    candidate_snapshot_id: SnapshotId
    model_version: str
    estimated_cost_usdt: DecimalString | None
    rejection_reason_codes: tuple[str, ...]
```

- A transfer intent is durably committed before any external POST. There is at most one confirmed daily-success intent for the configured policy key. Ambiguous outcomes require transfer-history reconciliation; they are not retried blindly.
- Rotation compares both assets using the same model version, horizon, timestamp rules, and score scale. Missing comparable data or cost estimate denies rotation.
- Reporting days remain UTC in this release. Alerts MAY render WIB, but a WIB accounting-boundary migration is separate and explicit.

## 11. Persistence, versioning, and audit

- Persistent records include `schema_version`. Readers support the immediately previous schema during a staged migration; writers emit only the current version after migration succeeds.
- SQLite migrations, when implemented, run transactionally and maintain a pre-migration backup/restore path. Decimal values are stored as strings, not REAL.
- Decision audit records contain snapshot ID, lifecycle/version, policy tier, reason code, deterministic-versus-advisory provenance, creation time, deadline, and resulting intent ID. They MUST NOT contain credentials or raw secrets.
- Compatibility migrations may read legacy protection fields, but MUST NOT fabricate unobserved peaks or claim guaranteed floors. Public/domain naming uses profit-floor trigger terminology.

## 12. Acceptance matrix

Downstream implementations and tests MUST demonstrate at minimum:

1. An `OrderbookData` dataclass carrying imbalance 1.5 is consumed without dictionary access and preserves 1.5; stale/failed data becomes `UNKNOWN`, never 1.0.
2. LONG and SHORT continuation interpret directional evidence separately; Spot depth cannot silently masquerade as Futures depth.
3. A `100 -> 106 -> 103` Spot path remains armed and triggers according to persisted trail even though current profit fell below activation.
4. A floor/SL breach during a delayed evaluation creates an exit immediately; a late `EXTEND` is discarded.
5. Evaluation exceeds 3 seconds, crashes, or returns invalid data: one durable `EXIT_PENDING` intent results, with no duplicate order.
6. Partial/ambiguous exit remains pending reconciliation; stale quantity is never resubmitted.
7. Futures first-observed profitable peak survives restart; LONG/SHORT and reopen lifecycles do not share protection state.
8. `spot_price_trail_fraction` and `futures_roe_drawdown_points` reject each other's units.
9. STRONG_BUY cannot suppress a stop or add to an existing position without shared risk approval; raw STRONG_SELL alone does not prove reversal.
10. Spot shortage or failed rotation cannot create a Futures entry; disabled Futures entries do not impair protection of existing Futures exposure.
11. Duplicate fills are idempotent, partial exits preserve inventory, and unknown commission valuation yields incomplete—not zero-fee—net PnL.
12. Entry denial and service/data failures fail closed without disabling exits, cancellations, or reconciliation.

## 13. Component boundaries

- Exchange adapters normalize symbols, units, IDs, timestamps, and raw execution events. They do not make strategy decisions.
- Snapshot builders validate freshness/provenance and produce immutable snapshots. They do not submit orders.
- Pure policies consume snapshots plus persisted lifecycle state and return decisions/intents. They perform no I/O.
- The protective executor owns precedence, durable intent creation, exchange submission, and reconciliation. It never waits for advisory evaluation.
- The accounting ledger owns fill idempotency, lot allocation, fee valuation status, and realized outcomes. Alerts and transfers consume ledger results; they do not recompute PnL.
- Background evaluators produce bounded advisory continuation results. They cannot mutate exchange state or protection directly.
- All external effects are behind adapters and must be mockable/denied in isolated tests.
