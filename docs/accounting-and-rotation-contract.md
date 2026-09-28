# Accounting, Funding Collection, and Capital Rotation Contract

Status: normative Phase 2 implementation contract  
Baseline: `cf1ae2c7cccb69e2429d22fbe83d5e2b4c85241e`  
ADR: `docs/adr/0001-profit-accounting-cutover.md`

The words MUST, MUST NOT, SHOULD, and MAY are normative. If this document conflicts with legacy code, the Phase 2 implementation follows this document while remaining disabled until its release gates pass.

## 1. Common representation and identifiers

- `DecimalString` is a finite, canonical base-10 string matching `^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`. Exponents, `NaN`, infinities, commas, leading plus signs, and negative zero are invalid. Quantities that cannot be negative use a non-negative subset.
- Implementations MUST construct `Decimal` from source strings, never through `Decimal(float)`.
- UTC instants use integer Unix epoch milliseconds. Reporting days use UTC `YYYY-MM-DD`.
- `account_id`, `venue`, and canonical `symbol` scope all exchange identifiers.
- IDs created internally SHOULD be UUIDv7/ULID or an equivalently collision-resistant sortable identifier. They are opaque to policy code.
- Serialized enum values are uppercase and unknown enum values fail closed.
- Raw exchange payloads and logs MUST redact credentials, signatures, and secrets.

## 2. Durable accounting contracts

Executable definitions live in `hermes/accounting/contracts.py`.

### 2.1 FillEvent

`FillEvent` represents exactly one exchange-confirmed fill, not an order request or aggregate order result.

Required fields:

- identity: `account_id`, `venue`, `symbol`, `trade_id`, `order_id`;
- time and side: `event_time_ms`, `side`;
- exact execution: `price`, `base_qty`, `quote_qty`;
- fee: `commission_asset`, `commission_qty`, `commission_usdt`, `valuation_status`;
- evidence: `source_payload_hash`.

Invariants:

1. Idempotency key is `(account_id, venue, symbol, trade_id)`.
2. `price`, `base_qty`, and `quote_qty` are positive. `commission_qty` is non-negative.
3. `quote_qty` is exchange-reported execution quantity, not caller price multiplied after the fact.
4. `commission_usdt` is present and non-negative only when valuation is `VALUED`. When valuation is unresolved it is `None`, not zero.
5. Identical duplicates are no-ops. Conflicting duplicates are quarantined and block distribution.
6. Fill ingestion is paginated until the configured range is demonstrably complete. A latest-100 response alone cannot establish completeness.

### 2.2 Lot, LotAllocation, and RealizedOutcome

A buy fill creates one or more durable lots after commission treatment. A sell consumes the oldest remaining lots first. The stable FIFO order is `(opened_at_ms, acquired_trade_id, lot_id)`.

A lot stores original and remaining base quantity, quote cost, allocated buy fee in USDT, and completeness. Values never become negative. A lot closes only at zero remaining quantity under the instrument's exact precision rules; dust remains explicit.

A `LotAllocation` links one sell fill to one acquired lot. Its allocated quantities, cost, and entry/exit fees are immutable after the outcome transaction commits. Corrections are append-only compensating/rebuild operations under a versioned reconciliation procedure, never silent row edits.

A `RealizedOutcome` is keyed to one sell fill and contains all lot allocations for that fill. For `VERIFIED` outcomes:

`net_pnl_usdt = gross_proceeds_usdt - fifo_cost_usdt - buy_fee_usdt - sell_fee_usdt`

Requirements:

- fees are included exactly once;
- a sell spanning multiple lots has one deterministic allocation sequence;
- partial sells preserve remaining lots;
- realized losses remain signed negative outcomes;
- a missing lot, history page, balance match, fee, or valuation yields `PARTIAL` or `UNRESOLVED`, with `net_pnl_usdt=None` and reason codes;
- an unresolved outcome never contributes a guessed positive or negative amount to distributable surplus; instead it blocks the policy interval.

### 2.3 AccountingSnapshot

The repository exposes an immutable `AccountingSnapshot` for policy evaluation. It includes:

- approved cutover identity and instant;
- signed cumulative verified net realized PnL since cutover;
- cumulative confirmed distributions since cutover;
- unresolved reason codes and reconciliation status;
- pending/unknown transfer indicators;
- snapshot time and ledger revision.

`distribution_surplus_usdt` is derived as verified net realized PnL minus confirmed distributions. Do not subtract losses twice: negative realized outcomes already form the loss carry.

A snapshot is distribution-complete only if all fills/outcomes in scope are verified, pagination is complete, fee valuation is complete, balance reconciliation passes, and no conflicting fill or unknown transfer exists.

## 3. Exactly-once daily Spot-to-Funding collector

### 3.1 Pure policy API

```text
evaluate_distribution(
    policy_config,
    accounting_snapshot,
    free_spot_usdt,
    total_equity_usdt,
    open_risk_usdt,
    now_ms,
) -> DistributionDecision
```

The function performs no I/O. It returns one of `PLAN_TRANSFER`, `NO_ACTION`, or `BLOCKED` and stable reason codes.

A `PLAN_TRANSFER` decision requires all of the following:

1. the collector is explicitly enabled and legacy auto-sweep is disabled;
2. an accounting cutover exists and is approved;
3. accounting and reconciliation are complete, with no unresolved contributing outcome;
4. there is no `SUBMITTING` or `UNKNOWN` transfer requiring reconciliation;
5. `distribution_surplus_usdt >= 1.00`;
6. there is no `CONFIRMED` distribution for the current UTC policy day;
7. `free_spot_usdt >= 1.00 + operational_buffer_usdt`;
8. post-transfer reserve satisfies `max(operational_buffer_usdt, total_equity_usdt * reserve_fraction + open_risk_usdt)` under the approved portfolio-reserve definition;
9. inputs are current, finite Decimal values; and
10. the repository can atomically claim the policy key.

The approved amount is exactly `1.00` USDT. The daily cap is one confirmed `1.00` USDT distribution per `(account_id, policy_version, reporting_day_utc)`. A rejected or final-failed intent does not consume the cap; an unknown intent blocks a replacement until reconciled.

### 3.2 Idempotency and persistence

Before any POST, the repository atomically creates or returns a unique intent for:

`(account_id, policy_version, reporting_day_utc)`

and a unique `client_transfer_id`. The policy snapshot stored with the intent includes ledger revision, surplus, free balance, reserve calculation, feature flags, and reason codes. A concurrent process receives the existing intent and cannot claim a second one.

The endpoint capability to echo/accept a client transfer ID MUST be verified against the deployed Binance API before relying on it. If unavailable, local uniqueness plus transfer-history matching is required; lack of a remote idempotency field does not permit blind retry.

### 3.3 Transfer lifecycle

States:

`PLANNED -> SUBMITTING -> CONFIRMED`

Exceptional transitions:

- `SUBMITTING -> UNKNOWN` for timeout, connection reset, malformed/ambiguous response, process recovery with no durable response, or any case where exchange acceptance cannot be disproved;
- `SUBMITTING -> FAILED_FINAL` only for a definitive exchange rejection proving no transfer occurred;
- any non-confirmed state -> `QUARANTINED` for conflicting evidence or invariant breach;
- `UNKNOWN -> CONFIRMED` when read-only history finds one exact match;
- `UNKNOWN -> FAILED_FINAL` only after an approved reconciliation window and authoritative history prove absence; automatic resubmission remains prohibited unless the intent is explicitly made retryable by the reconciler.

`CONFIRMED`, `FAILED_FINAL`, and `QUARANTINED` are terminal for submission. A confirmed intent is never resent.

### 3.4 External operation sequence

1. In one database transaction, evaluate against a fixed ledger revision and create/claim `PLANNED`.
2. Persist `SUBMITTING` and commit before calling Binance.
3. POST exactly `1.00` USDT, asset `USDT`, direction `MAIN_FUNDING`.
4. Persist returned evidence. A returned `tranId` is not by itself policy completion.
5. Query read-only universal-transfer history.
6. Match account, asset, direction, exact Decimal amount, transfer/client ID when available, and a bounded time window. Zero matches leaves `UNKNOWN`; more than one plausible match is `QUARANTINED`.
7. Mark `CONFIRMED` only after exactly one match. The confirmed ledger query, not a JSON boolean, consumes the daily cap.

On startup and before creating a new intent, reconcile `SUBMITTING` and `UNKNOWN` intents first. The executor MUST NOT hold a SQLite write transaction open during network I/O.

## 4. Cost-aware capital rotation

### 4.1 Pure comparative policy

Rotation compares a held asset and replacement candidate using immutable snapshots with the same:

- model and schema version;
- feature definitions and score scale;
- required timeframes and closed-bar rules;
- maximum age and observation-time rules;
- market/venue semantics.

Synthetic neutral RSI values, confidence-to-score conversion, caller defaults, stale cache fallback, and unlike scoring paths are prohibited. Missing or stale required evidence yields `NO_ROTATION`.

A decision records score edge and monetary cost separately. USDT costs MUST NOT be subtracted from a dimensionless score.

`APPROVE` requires every gate:

1. `ROTATION_ENABLED` is true for execution; shadow evaluation may run while it is false but has no executor reference;
2. symmetric fresh snapshots and sustained candidate confirmation across distinct closed bars;
3. candidate score edge meets the versioned threshold;
4. the held position is not protected, `RIDING`, `TP_EVALUATING`, `EXIT_PENDING`, or subject to another intent;
5. held PnL is within the approved rotation band and minimum hold is met;
6. spread, depth, min-notional, and liquidity gates pass for both legs;
7. estimated sell fee + spread/slippage plus buy fee + spread/slippage is present and within its configured cost budget;
8. expected benefit and stop-risk budgets pass as distinct documented gates;
9. the centralized post-sale portfolio risk gate approves the replacement;
10. durable cooldown and daily attempt limits allow an attempt; and
11. the position lifecycle/version and candidate snapshot are unchanged at execution revalidation.

Ranking among eligible held positions is deterministic: highest score edge, then highest expected net benefit, then lowest estimated cost fraction, then canonical symbol as stable tie-breaker. Ranking never bypasses a gate.

`RotationDecision` is immutable and auditable. Rejection reason codes are stored even in shadow mode. The shadow recorder may persist decisions and hypothetical outcomes but MUST have no import/reference path to order or transfer adapters.

### 4.2 Rotation intent and idempotency

One decision may create at most one intent (`decision_id` unique). One position lifecycle may have at most one nonterminal rotation or exit intent. Claiming, cooldown start, and daily attempt increment are atomic. Cooldown begins when the sell becomes externally possible (`SELL_SUBMITTING`), not only after a successful buy, so a crash or failed replacement cannot evade limits.

States:

`PLANNED -> SELL_SUBMITTING -> SELL_FILLED -> BUY_SUBMITTING -> COMPLETED`

Recovery states:

- `SELL_SUBMITTING -> SELL_UNKNOWN` on ambiguity;
- `SELL_UNKNOWN -> SELL_FILLED` only after order/fill read-back;
- `SELL_FILLED -> CASH_RECOVERY` when no safe replacement can proceed;
- `BUY_SUBMITTING -> BUY_UNKNOWN` on ambiguity;
- `BUY_UNKNOWN -> COMPLETED` only after order/fill read-back and position reconciliation;
- any unsafe conflict -> `QUARANTINED`.

No state with an unknown order may issue another order. Partial sell fills are ingested and reconciled; the executor may continue only under an explicit versioned partial-fill policy and may never oversell. Unknown or rejected policy values fail to `CASH_RECOVERY`/`QUARANTINED`, not to a new asset.

### 4.3 Sell-then-buy execution

1. Claim the persisted approved decision.
2. Revalidate position lifecycle/version, no protective exit, snapshots, balances, filters, costs, and risk.
3. Persist `SELL_SUBMITTING`, commit, then submit one Spot sell.
4. Resolve the sell from read-only order/trade history and ingest actual fills.
5. Compute actual freed USDT from verified sell fills and fees.
6. Replacement budget is `min(actual_net_freed_usdt - required_reserve, approved_replacement_budget)` and must satisfy exchange filters.
7. Fetch a fresh candidate snapshot and rerun costs and centralized risk after the sale.
8. If invalid, insufficient, rejected, or definitively failed, persist `CASH_RECOVERY` and retain USDT.
9. Persist `BUY_SUBMITTING`, commit, then submit one Spot buy.
10. Resolve unknown responses via read-only history. Mark `COMPLETED` only after fill ingestion and position reconciliation.

There is no Futures fallback, alternative-coin fallback, all-balance spend, or automatic “rescue” buy. Existing protective TP/SL/trailing and signal precedence outrank rotation throughout.

## 5. Repository and adapter interfaces

The implementation MUST preserve these boundaries:

- `AccountingRepository`: fill upsert/conflict detection, transactional FIFO allocation, immutable snapshots, cutover state, and reconciliation blockers.
- `TransferIntentRepository`: atomic daily claim, lifecycle transitions with compare-and-set expected state, unresolved-intent listing, and confirmed-distribution totals.
- `RotationRepository`: decision append, atomic intent claim, lifecycle compare-and-set, persistent cooldown/attempt counters, and nonterminal conflict lookup.
- `TransferGateway`: one narrowly scoped Spot-to-Funding submission plus read-only history. It accepts Decimal strings and returns typed evidence, not truth inferred from exceptions.
- `SpotOrderGateway`: submit one Spot sell/buy and perform read-only order/trade lookup. It has no Futures method.
- policies: pure functions with no repository, network, Telegram, filesystem, or clock reads; time is an argument.
- executors: orchestration only; they do not recompute ledger PnL or strategy scores.

Every transition method takes `expected_status` and fails on stale state. External exchange IDs are unique within account/venue. State changes and audit rows commit together.

## 6. Test isolation contract

All automated tests are credential-free and default-deny. The deny guards activate before application imports wherever feasible and remain active unless the test replaces a boundary with an in-memory fake. “Mocking” a high-level call must not disable global guards.

### 6.1 Network and subprocess

Tests MUST deny by default:

- socket connect/connect_ex/create_connection and async equivalents used by the project;
- urllib and `requests` sends;
- network subprocesses such as curl/wget/nc;
- DNS/network clients introduced later.

Loopback is denied unless a narrowly scoped test fixture explicitly registers an ephemeral local endpoint. No allowlist may include Binance or Telegram hosts.

### 6.2 Binance mutation

A dedicated guard MUST reject any unmocked authenticated mutation, even if network code is monkeypatched. At minimum it denies POST/PUT/DELETE/PATCH through `binance_signed_request`, legacy wrappers, transfer gateways, and order gateways. Tests use typed fakes injected at adapter boundaries; they do not patch the global guard away.

Read-only Binance behavior is represented by deterministic fixtures. Unit and ordinary integration tests do not call live GET endpoints either because the network guard remains active.

### 6.3 Telegram

Telegram dispatch is replaced with a recording fake or explicitly rejected before HTTP construction. A test asserting alert content inspects recorded messages. No test may treat “network exception caught and False returned” as sufficient dispatch isolation.

### 6.4 Production environment and files

Before importing `hermes.config`, tests MUST set an isolation marker and temporary state/database paths. Under that marker, configuration MUST NOT read the repository/production `.env`; attempted reads raise `ProductionFileAccessError` rather than returning secrets.

The file guard denies read, write, rename, replace, link, chmod, truncate, and delete access to production `.env` and denies mutation of production state/database paths. SQLite connection factories reject paths outside the registered isolated directory, including URI paths and symlink escapes. Tests must never copy production `.env` or live databases into a worker.

Allowed persistent files live under the per-test temporary directory beneath `$TMPDIR`. `ACCOUNTING_DB_PATH` is explicitly injected. Teardown restores environment, module paths, clocks, and shared singletons.

### 6.5 Required sentinel tests

The suite MUST prove:

1. socket/urllib/requests and network subprocess access fail;
2. direct and wrapped Binance POSTs fail before transport;
3. Telegram dispatch fails before transport and recording fakes work;
4. production `.env` read and every protected-file mutation class fail;
5. SQLite outside the registered temporary root and symlink escapes fail;
6. all configured state and ledger paths resolve inside the isolated root;
7. guards remain effective when an application adapter catches exceptions;
8. duplicate fills/intents, concurrent claims, crash points, and restart recovery preserve idempotency;
9. timeout-after-success fixtures reconcile without a second POST; and
10. shadow rotation emits zero order/transfer calls.

A test requiring real network or authenticated credentials is not part of the default suite and requires a separate explicit operator-approved harness. No live transfer is manufactured for verification.

## 7. Stable reason codes

Implementations MAY add versioned codes but MUST preserve these semantics:

Distribution rejection/blocking:

- `FEATURE_DISABLED`, `CUTOVER_NOT_APPROVED`, `ACCOUNTING_INCOMPLETE`, `RECONCILIATION_REQUIRED`, `UNKNOWN_TRANSFER`, `SURPLUS_BELOW_TARGET`, `DAY_ALREADY_CONFIRMED`, `INSUFFICIENT_FREE_USDT`, `RESERVE_BREACH`, `CLAIM_CONFLICT`, `INVALID_INPUT`.

Rotation rejection/recovery:

- `FEATURE_DISABLED`, `SHADOW_ONLY`, `STALE_SNAPSHOT`, `INCOMPARABLE_SNAPSHOT`, `INSUFFICIENT_SCORE_EDGE`, `CONFIRMATION_MISSING`, `PROTECTED_POSITION`, `PNL_OUTSIDE_BAND`, `MIN_HOLD_NOT_MET`, `LIQUIDITY_REJECTED`, `COST_BUDGET_EXCEEDED`, `RISK_REJECTED`, `COOLDOWN_ACTIVE`, `DAILY_ATTEMPT_LIMIT`, `LIFECYCLE_CHANGED`, `UNKNOWN_ORDER`, `RETAINED_USDT`, `INVARIANT_BREACH`.

Unknown reason codes deny new risk/action and remain visible in audit output.

## 8. Minimum acceptance scenarios

Accounting:

- multiple buys followed by partial sells allocate FIFO exactly;
- base, quote, and third-asset fees are deducted once;
- `+1.20` then `-2.00` yields `-0.80` and no transfer;
- `-2.00` then `+3.20` yields `+1.20` and is eligible once if every other gate passes;
- gross `1.01` reduced below `1.00` by fees is ineligible;
- duplicate/out-of-order fills replay identically;
- unresolved commission or pagination gap blocks collection.

Collector:

- two concurrent ticks create one daily intent;
- crash before POST is recoverable without assuming submission;
- timeout after exchange success becomes `UNKNOWN`, read-back confirms it, and no second POST occurs;
- an ambiguous or duplicate history match quarantines the intent;
- midnight UTC creates a new policy day but does not ignore prior loss carry or unresolved intents;
- deposits and Spot/Funding movements never count as PnL.

Rotation:

- held and candidate evaluation is symmetric and deterministic;
- stale/missing data produces no rotation, not neutral defaults;
- costs and scores remain separate;
- restart does not reset cooldown/attempt count;
- a protective exit prevents sell submission;
- partial/unknown sell and unknown buy reconcile without duplicate orders;
- candidate invalidation or failed buy produces `CASH_RECOVERY` with USDT retained;
- no path invokes Futures;
- shadow mode records decisions with zero execution-adapter calls.

## 9. Explicit non-goals

This contract does not change current TP, SL, trailing, or signal precedence; enable Futures; promise profitability; authorize deployment; treat Funding as risk-free; or authorize retrospective distribution from unverifiable legacy profit.