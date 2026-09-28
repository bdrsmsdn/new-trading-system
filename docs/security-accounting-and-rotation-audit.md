# Formal Security Audit Report: Phase 2 Accounting, Sweep Collector, and Capital Rotation

**Auditor:** Application Security Engineer & AppSec Auditor (`sec`)  
**Date:** 2026-09-28  
**Scope:** Phase 2 FIFO Accounting Engine, Spot-to-Funding Daily Profit Sweep Collector, Cost-Aware Capital Rotation State Machine, SQLite Persistence Layer, API Permission Boundaries, and Secret Leakage Prevention.  
**Branch:** `wt/t_50b322d0` (baseline: `cf1ae2c7cccb69e2429d22fbe83d5e2b4c85241e`, parent: `979357feae299d70dc88fb90b8ec455a2c7c9f3c`)  
**Security Verdict:** **PASS (CLEAN)**  

---

## 1. Executive Summary

A comprehensive application security audit and vulnerability assessment was conducted across all newly developed Phase 2 domain components in the Hermes Trading System. The audit specifically focused on preventing financial exploits, secret leakage, authorization bypasses, race conditions, replay attacks, and unauthorized exchange privileges.

### Core Audit Outcomes:
1. **Transfer Authorization & Surplus Guardrails:** Spot-to-Funding profit transfer logic cannot be manipulated, triggered without human cutover approval, or executed beyond verified cumulative net surplus thresholds.
2. **Zero Secret Leaks & Hardened Persistence:** API keys, HMAC secrets, and bearer tokens are never logged, serialized into SQLite, or leaked during test execution. SQLite database files and WAL companions are strictly enforced with `0600` POSIX file permissions.
3. **Idempotency & Replay Defense:** All fill ingestion, transfer claiming, and rotation execution paths enforce deterministic hashing, collision detection with automated quarantine, and compare-and-set (CAS) state machine transitions. Network timeouts and daemon restarts recover cleanly via read-only reconciliation without duplicate transactions.
4. **Strict Isolation & Zero Futures Fallback:** Futures API endpoints and unauthorized transfer types remain completely blocked. Capital rotation failures enter `CASH_RECOVERY` retaining USDT in Spot with zero fallback to Futures.

---

## 2. Scope of Audited Components

| Module | Primary Functionality | Key Security Controls Audited |
| :--- | :--- | :--- |
| `hermes/accounting/collector.py` | Spot-to-Funding Daily Profit Collector | Pure 7-gate distribution policy, read-only reconciliation, `MAIN_FUNDING` transfer gateway. |
| `hermes/accounting/contracts.py` | Phase 2 Domain Types & Protocols | Immutable frozen dataclasses, strict `canonical_decimal` boundary validation, state enums. |
| `hermes/accounting/ingestion.py` | Fill Ingestion & Fee Valuation | Secret redaction in `_hash_trade_payload`, deterministic fee valuation, duplicate detection. |
| `hermes/accounting/repository.py` | Durable SQLite Ledger & Repositories | FIFO allocation, atomic CAS transitions, conflicting fill quarantine, reconciliation blockers. |
| `hermes/accounting/rotation.py` | Cost-Aware Capital Rotation | Score-cost separation, symmetric snapshot validation, `CASH_RECOVERY` fallback, Spot-only order gateway. |
| `hermes/accounting/schema.py` | SQLite Migrations & Durability | WAL mode, foreign key enforcement, SHA256 migration checksums, atomic backups, `0600` permissions. |
| `hermes/api/transfer.py` | Transfer API Adapters | Explicit amount bounding, safe logging, legacy state archival. |
| `hermes/config.py` | System Configuration & Risk Gates | Fail-safe boolean environment parsing (`parse_bool_env`), default-disabled flags. |
| `tests/support/isolation.py` | Test Isolation Sentinels | Global blocking of unmocked network sockets, requests, urllib, and subprocess curl. |

---

## 3. Deep-Dive Security Verification & Checkpoints

### 3.1 Checkpoint 1: Spot-to-Funding Transfer Authorization Logic

* **Threat Model:** Unauthorized fund extraction, negative-PnL distribution leakage, premature transfer before fee settlement, or portfolio cash starvation through excessive sweep.
* **Evaluation Pipeline (`evaluate_distribution` in `collector.py`):**
  A pure, side-effect-free policy evaluates 7 sequential security gates before authorizing any transfer:
  1. **Gate 1 (Feature Flags):** Checks `policy_config.enabled == True` and `policy_config.legacy_auto_sweep_enabled == False`.
  2. **Gate 2 (Cutover Approval):** Verifies `accounting_snapshot.cutover_status == CutoverStatus.APPROVED`. If cutover is pending or rejected, distribution is blocked fail-closed.
  3. **Gate 3 (Accounting Completeness & Reconciliation):** Requires `completeness == Completeness.VERIFIED`, `reconciliation_status == ReconciliationStatus.RECONCILED`, and zero unresolved reason codes (e.g. `UNVALUED_FEES_EXIST`).
  4. **Gate 4 (Active/Unknown Transfer Gate):** If any in-flight transfer exists in `SUBMITTING` or `UNKNOWN` status, planning is blocked until read-only reconciliation completes.
  5. **Gate 5 (Surplus Threshold):** Enforces `distribution_surplus_usdt >= target_usdt` (1.00 USDT). Losses carry forward signed; unrecovered net losses strictly block surplus accumulation.
  6. **Gate 6 (Free Spot Balance Availability):** Requires `free_spot_usdt >= (target_usdt + operational_buffer_usdt)`.
  7. **Gate 7 (Portfolio Reserve Compliance):** Enforces that post-transfer Spot balance satisfies `free_spot - target >= max(operational_buffer, total_equity * reserve_fraction + open_risk)`.
* **Amount Invariant:** Authorized transfer amount is strictly bounded to `canonical_decimal(policy_config.target_usdt)` (exactly 1.00 USDT), eliminating user or LLM payload injection risks.

### 3.2 Checkpoint 2: API Credential Handling & Persistence Security

* **Threat Model:** API key or HMAC signature leakage via log files, exception tracebacks, SQLite ledger serialization, or test environment exfiltration.
* **Audit Findings:**
  * **Payload Hash Scrubbing (`_hash_trade_payload` in `ingestion.py`):** Explicitly scrubs `signature`, `apikey`, `api_key`, `secret`, and `token` keys from dictionary structures before generating SHA-256 integrity hashes.
  * **Safe URL Logging (`hermes/api/auth.py`):** Request tracking logs strip query parameters, request bodies, and HMAC signatures, logging only the base endpoint (e.g. `https://api.binance.com/api/v3/order`).
  * **Database Column Audit:** Inspection of all SQLite tables (`fills`, `lots`, `realized_outcomes`, `lot_allocations`, `transfer_intents`, `rotation_decisions`, `rotation_intents`, `reconciliation_blockers`) confirmed zero credential fields.
  * **POSIX File Permissions (`schema.py`):** SQLite database files, atomic pre-migration backup files, and WAL/SHM companion files are enforced with strict `0600` permissions (readable and writable only by the owner).
  * **Test Isolation Sentinels (`isolation.py`):** Unmocked network sockets, requests sessions, urllib handlers, and curl subprocesses raise `NetworkAccessBlockedError` immediately if invoked in tests.

### 3.3 Checkpoint 3: Idempotency, Replay Defense, and Crash Recovery

* **Threat Model:** Double-spending via duplicate HTTP requests, replayed fill events, race conditions during daemon restart, or conflicting database transitions.
* **Audit Findings:**
  * **Conflicting Fill Quarantine:** If a fill with an existing `(account_id, venue, symbol, trade_id)` arrives with an altered payload hash, `SqliteAccountingRepository.ingest_fill` immediately sets `is_quarantined = 1`, records a persistent `reconciliation_blocker`, and raises `DuplicateFillConflictError`. This blocks all snapshot distributions fail-closed.
  * **Atomic Daily Claim (`claim_daily_intent`):** Enforces a database UNIQUE constraint on `(account_id, policy_version, reporting_day_utc)` and generates a unique `client_transfer_id` (`tf_<YYYYMMDD>_<uuid>`). Re-invocations on the same day safely return the existing intent without creating duplicate transfers.
  * **Compare-And-Set (CAS) Lifecycle:** Transitions in `transfer_intents` and `rotation_intents` check `expected_status`. Out-of-order or stale transitions raise `ConcurrencyConflictError`.
  * **Read-Only Crash Recovery (`reconcile_unresolved_transfers`):** If a transfer times out or the daemon restarts during `SUBMITTING` or `UNKNOWN`, the collector queries Binance transfer history (`/sapi/v1/asset/transfer` GET) using read-only polling. If exactly one matching record is found, it confirms the transfer; if zero matches are found after timeout, it transitions to `FAILED_FINAL`; if ambiguous multiple matches are found, it quarantines the intent.

### 3.4 Checkpoint 4: Futures APIs and Unauthorized Endpoint Isolation

* **Threat Model:** Unintended order routing to high-risk derivative venues, leverage escalation, or execution via unapproved transfer types.
* **Audit Findings:**
  * **Fail-Safe Config Defaults:** `FUTURES_ENABLED`, `ROTATION_ENABLED`, `DAILY_PROFIT_COLLECTION`, and `AUTO_SWEEP_PROFIT_TO_FUNDING` default strictly to `False` using `parse_bool_env`.
  * **Spot-Only Order Gateway (`BinanceSpotOrderGateway` in `rotation.py`):** Strictly encapsulates `/api/v3/order` (Spot). Contains zero imports, routes, or methods for Futures (`/fapi/*` or `/dapi/*`).
  * **Restricted Transfer Types (`BinanceTransferGateway` in `collector.py`):** Strictly hardcodes `type: "MAIN_FUNDING"` (Spot to Funding). External withdrawals or futures transfers (`MAIN_UMFUTURE`, `MAIN_CMFUTURE`) cannot be executed.
  * **CASH_RECOVERY Safety Net:** During capital rotation, if the replacement buy fails, is rejected by Binance (e.g. `-2010:INSUFFICIENT_FUNDS`), or fails preflight validation, the rotation state machine transitions to `CASH_RECOVERY`. The liquidated capital is safely retained as Spot USDT with zero Futures fallback.

---

## 4. Automated Security Verification Matrix

| Test Module | Test Method | Invariant Verified | Result |
| :--- | :--- | :--- | :--- |
| `test_security_accounting_audit.py` | `test_surplus_below_target_strictly_refuses_transfer` | Surplus < 1.00 USDT returns `NO_ACTION` | **PASS** |
| `test_security_accounting_audit.py` | `test_unverified_accounting_or_loss_carries_blocks_transfer` | Incomplete fee valuation / loss carry blocks transfer | **PASS** |
| `test_security_accounting_audit.py` | `test_cutover_not_approved_blocks_transfer` | Cutover pending/rejected fails closed to `BLOCKED` | **PASS** |
| `test_security_accounting_audit.py` | `test_portfolio_reserve_breach_blocks_transfer` | Transfer breaching 25% cash reserve fails closed | **PASS** |
| `test_security_accounting_audit.py` | `test_in_flight_or_unknown_transfer_blocks_evaluation` | Active `UNKNOWN`/`SUBMITTING` transfer blocks new planning | **PASS** |
| `test_security_accounting_audit.py` | `test_trade_payload_hash_scrubs_secrets` | Redaction of API keys, signatures, and tokens in payload hashes | **PASS** |
| `test_security_accounting_audit.py` | `test_sqlite_db_and_companions_have_0600_permissions` | Database and WAL companion permissions set to `0600` | **PASS** |
| `test_security_accounting_audit.py` | `test_zero_secrets_serialized_in_ledger_tables` | Verification that zero secret columns exist in SQLite schema | **PASS** |
| `test_security_accounting_audit.py` | `test_conflicting_fill_duplicate_triggers_immediate_quarantine` | Fill tampering triggers `is_quarantined=1` & blocker | **PASS** |
| `test_security_accounting_audit.py` | `test_daily_transfer_intent_exactly_once_enforcement` | Unique daily transfer constraint prevents duplicate sweeps | **PASS** |
| `test_security_accounting_audit.py` | `test_out_of_order_cas_transition_rejected` | Stale CAS state transition raises `ConcurrencyConflictError` | **PASS** |
| `test_security_accounting_audit.py` | `test_futures_enabled_disabled_by_default_in_config` | `FUTURES_ENABLED=False` default config verification | **PASS** |
| `test_security_accounting_audit.py` | `test_spot_order_gateway_has_zero_futures_endpoints` | Absence of Futures endpoints in Spot order gateway | **PASS** |
| `test_security_accounting_audit.py` | `test_transfer_gateway_strictly_limited_to_main_funding` | Gateway strictly enforces `MAIN_FUNDING` universal transfer | **PASS** |
| `test_security_accounting_audit.py` | `test_rotation_cash_recovery_retains_usdt_with_zero_futures_fallback` | Buy rejection enters `CASH_RECOVERY` retaining Spot USDT | **PASS** |

**Test Execution Summary:** 168 passing tests (0 failures, 0 errors, 0 skips).

---

## 5. Conclusion & Transition Sign-Off

The Phase 2 FIFO accounting, profit sweep collector, and cost-aware rotation implementations adhere strictly to the principle of least privilege, zero-trust input validation, defense-in-depth, and fail-closed execution. No secret leaks, unauthorized endpoints, or transfer race conditions exist.

**Final Security Assessment:** **APPROVED FOR CODE REVIEW AND CUTOVER READINESS (STAGE 2 SIGN-OFF).**
