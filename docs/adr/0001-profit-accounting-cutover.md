# ADR 0001: Cut over profit authority to a durable fill ledger

- Status: Accepted for Phase 2 implementation
- Date: 2026-09-28
- Baseline: `cf1ae2c7cccb69e2429d22fbe83d5e2b4c85241e`
- Decision owners: Architecture / Product Owner
- Scope: Spot fill accounting, profit distribution, capital rotation, and isolated verification

## Context

The current `daily_profit_state.json` counter records only positive caller-supplied outcomes, uses binary floating point, and marks a day collected only after an external transfer returns. It cannot establish cost basis, fees, losses, partial fills, concurrent collector ownership, or whether a timed-out transfer succeeded. Existing rotation similarly performs a sell and buy without a durable two-leg intent.

These weaknesses make the JSON counter unsuitable as an authority for moving money. They also make a restart or ambiguous exchange response capable of producing an unaccounted half-complete action.

## Decision

### 1. Authority and cutover

A transactional SQLite ledger driven by actual Binance fills is the sole authority for inventory, realized PnL, distribution eligibility, transfer intents, and rotation intents.

`daily_profit_state.json` is archival, unverified, and non-authoritative from the Phase 2 cutover onward. It MAY be copied or displayed with an `UNVERIFIED_LEGACY` label. No value, flag, or date in that file may authorize a transfer, seed distributable surplus, create a lot, or satisfy reconciliation.

The cutover is explicit and durable. An approved cutover record contains account, venue, cutover instant, baseline reference, backfill range, and approval status. Distribution remains blocked until:

1. the cutover is approved;
2. accessible fill history is fully paginated and ingested;
3. exchange balances reconcile to open lots within exchange precision; and
4. every realization contributing to surplus has complete cost and fee valuation.

An asset with incomplete history remains bot-owned, not “manual.” Its inventory and affected outcomes are `UNRESOLVED`; this blocks distribution but does not block protective selling. Any resulting sell remains unresolved until its basis is established.

### 2. Accounting model

All monetary and quantity arithmetic uses `decimal.Decimal` created from source strings. SQLite stores canonical base-10 strings in `TEXT` columns and MUST NOT use `REAL` for ledger values. Rounding occurs only at an exchange filter or reporting boundary and records the applied rule.

A fill is uniquely identified by `(account_id, venue, symbol, trade_id)`. Re-ingesting an identical fill is a no-op. A different payload for an existing key is quarantined and creates a reconciliation blocker. The source payload hash excludes secrets and provides conflict evidence.

Spot buys create FIFO lots. Spot sells consume lots in `(opened_at_ms, acquired_trade_id, lot_id)` order within the same account, venue, and symbol. Allocation and outcome creation occur in one database transaction. A partial sell consumes only the allocated quantity; a partial lot remains open.

Fees are deducted exactly once:

- quote-asset fees use the exchange-reported quote amount;
- base-asset fees alter effective received/disposed inventory and are valued from the fill price when that is an exact same-fill conversion;
- BNB or other-asset fees require a documented execution-time USDT valuation and provenance;
- missing or stale valuation produces `UNRESOLVED`, never a zero fee.

For a verified sell outcome:

`net_pnl_usdt = gross_proceeds_usdt - fifo_cost_usdt - buy_fee_usdt - sell_fee_usdt`

Every consumed opening fill, closing fill, fee, and allocation must be traceable. Losses are ordinary negative outcomes and are never filtered out.

For the approved cutover scope, cumulative net realized PnL is the signed sum of all `VERIFIED` post-cutover outcomes. Confirmed distributions are then subtracted once:

`distribution_surplus = cumulative_verified_net_realized_pnl - cumulative_confirmed_distributions`

This signed sum already carries prior losses forward; a separate numeric “loss carry” MUST NOT be subtracted again. Any `PARTIAL` or `UNRESOLVED` event in the accounting interval is a blocker rather than an estimated deduction.

### 3. Reporting boundary

Accounting instants are UTC. `event_time_ms` is Unix epoch milliseconds. The initial distribution policy day is the half-open UTC interval `[00:00:00Z, next 00:00:00Z)`, serialized as `YYYY-MM-DD`. WIB is display-only. Changing the accounting timezone requires a new versioned policy and migration.

### 4. Transaction and concurrency boundary

SQLite uses foreign keys, WAL mode, a bounded busy timeout, and explicit transactions. One writer atomically claims a collector or rotation intent before any external mutation. Database commit and exchange POST cannot be one atomic transaction, so external action state is modeled explicitly and reconciled.

Repository methods own persistence transactions and canonical Decimal conversion. Pure policy functions perform no I/O. Exchange adapters normalize API payloads and perform GET/POST calls but do not decide eligibility. Executors consume persisted decisions/intents and are the only components allowed to invoke mutating adapters.

### 5. Safe defaults and release stages

`DAILY_PROFIT_COLLECTION=False`, `AUTO_SWEEP_PROFIT_TO_FUNDING=False`, `ROTATION_ENABLED=False`, and Futures entry disablement remain defaults. Implementing or testing a feature does not activate it.

Funding collection requires migration, reconciliation, QA/security/reviewer gates, and separate deployment approval. Rotation ships as deterministic replay and zero-side-effect production shadow first; activation requires prospective evidence and separate PO approval. A failed replacement buy retains USDT. Rotation must never route to Futures or an alternative asset.

## Alternatives rejected

1. Repair the JSON counter. Rejected because it cannot provide transactional fill idempotency, FIFO traceability, action state, or concurrent ownership.
2. Infer PnL from position average and caller price. Rejected because partial fills, commissions, historical losses, and timeout-after-fill cases are not authoritative.
3. Retry transfer/order timeouts immediately. Rejected because a timeout may occur after exchange success and cause a duplicate action.
4. Use floats with display rounding. Rejected because representation and accumulated allocation error are unacceptable for a monetary ledger.
5. Enable rotation when unit tests pass. Rejected because policy value requires replay and prospective shadow evidence; implementation correctness is not profitability evidence.

## Consequences

Positive consequences:

- realized outcomes are reproducible from exchange fills;
- losses, fees, partial fills, and restarts have explicit semantics;
- collector and rotation actions are auditable and recoverable;
- unknown exchange outcomes fail closed without pretending failure;
- tests can prove absence of live side effects.

Costs and limitations:

- historical gaps or unvalued fees intentionally block distributions;
- backfill and reconciliation require pagination and additional storage;
- SQLite serializes writers and requires disciplined transaction duration;
- exactly-once means one confirmed economic effect under modeled retries, not an atomic transaction across SQLite and Binance;
- no return, profit, principal, or Funding-wallet safety is guaranteed.

## Migration and rollback

1. Keep collection and rotation disabled.
2. Create an atomic backup and apply schema migrations transactionally.
3. Record a pending cutover; ingest paginated fills and construct lots.
4. Reconcile balances and produce an evidence report.
5. Approve the cutover only after unresolved blockers are cleared.
6. Enable the collector only under a later approved deployment. Rotation remains shadow.

Rollback disables new collection/rotation claims first. It MUST NOT restore an older database over fills or intents written after deployment. Reconcile forward, preserve the ledger for audit, and return unknown external actions to read-only reconciliation. The legacy JSON file is never promoted back to authority.

## Normative companion

Detailed data, policy, lifecycle, adapter, and test-isolation contracts are in `docs/accounting-and-rotation-contract.md`. Executable interface types are in `hermes/accounting/contracts.py`.
