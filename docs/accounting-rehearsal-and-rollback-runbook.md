# Accounting DB Rehearsal, Backup, and Rollback Runbook

```
================================================================================
STATUS: NOT DEPLOYED (Release Candidate Stage)
ZERO AUTOMATED DEPLOYMENT OR SERVICE RESTARTS SCHEDULED WITHOUT EXPLICIT PO APPROVAL
Target: Hermes Autonomous Trading System (Phase 2 Accounting & Rotation Wiring)
Role: Senior DevOps & Site Reliability Engineer (Infra Specialist)
Date: 2026-09-28
================================================================================
```

---

## 1. Overview & Operational Principles

This runbook defines the operational procedures, migration verification steps, dry-run reconciliation rehearsal drills, automated pre-cutover backup/restore routines, and emergency rollback protocols for the Phase 2 Accounting Ledger and Shadow Capital Rotation runtime integration.

### Core SRE & Operational Principles
1. **Zero Production Mutation During Rehearsal:** All rehearsal, schema migration verification, and trade reconciliation drills MUST execute against dedicated, isolated database paths using `--db-path`. Production databases (`hermes_ledger.db`) and live running services MUST remain untouched.
2. **Fail-Closed Accounting Safety:** Ledger cutover starts in `PENDING` state and cannot be self-approved by feature flags. The daily profit distribution collector strictly fails closed to `BLOCKED` until a cutover record is explicitly approved by authorized operational sign-off.
3. **Atomic Online Backups:** Pre-cutover backups use SQLite's native online Backup API (`sqlite3.backup`), ensuring consistent snapshots with 0600 file permissions and SHA-256 validation without locking or interrupting active readers.
4. **Strict Rollback Triggers:** Any FIFO mismatch, trade pagination gap, fee valuation failure, or transfer ambiguity triggers an immediate fail-back to legacy safe mode.
5. **STATUS NOT DEPLOYED:** This release candidate is strictly staged. No production deployment, systemd restart, or cutover execution shall occur without explicit Product Owner (PO) approval.

---

## 2. Environment & CLI Operational Tooling

The operational tooling is exposed through the Hermes CLI subcommands (`hermes/accounting/ops.py` via `hermes/cli.py`):

| Subcommand | Purpose | Production-Safe |
| :--- | :--- | :--- |
| `hermes accounting-init` | Initialize isolated SQLite accounting DB and execute SQL migrations | YES (with `--db-path`) |
| `hermes accounting-verify` | Verify DB integrity, foreign keys, migration checksums, and permissions | YES |
| `hermes accounting-backup` | Create atomic online backup of DB and legacy state files with SHA-256 | YES |
| `hermes accounting-restore` | Restore DB from backup file with preflight & post-flight integrity checks | YES (with `--db-path`) |
| `hermes accounting-reconcile`| Run dry-run reconciliation rehearsal on archived trades (zero mutations) | YES |
| `hermes accounting-status` | Report cutover state, verified PnL, surplus, and active blockers | YES |

---

## 3. Isolated DB Initialization & Migration Verification (AC 1)

### 3.1 Initializing an Isolated Database
To verify migrations and schema initialization in an isolated environment without altering production state, execute:

```bash
# Set isolated rehearsal workspace
export REHEARSAL_DB_DIR="/home/badra/.hermes/profiles/infra/cache/scratch/rehearsal"
mkdir -p "${REHEARSAL_DB_DIR}"
chmod 0700 "${REHEARSAL_DB_DIR}"

# Initialize isolated SQLite database
python3 -m hermes.cli accounting-init \
  --db-path "${REHEARSAL_DB_DIR}/isolated_ledger.db" \
  --account-id "default"
```

**Expected JSON Output:**
```json
{
  "status": "INITIALIZED",
  "db_path": ".../isolated_ledger.db",
  "file_exists": true,
  "file_size_bytes": 258048,
  "applied_versions": [1],
  "tables_count": 13,
  "tables": [
    "accounting_cutovers",
    "external_cash_flows",
    "fills",
    "lot_allocations",
    "lots",
    "realized_outcomes",
    "reconciliation_blockers",
    "rotation_audit_logs",
    "rotation_decisions",
    "rotation_intents",
    "schema_migrations",
    "transfer_intents",
    "transfer_reconciliation_logs"
  ],
  "cutover_id": "cutover_default_<timestamp>",
  "cutover_status": "PENDING",
  "journal_mode": "WAL",
  "foreign_keys": "ON",
  "file_permissions": "0600"
}
```

### 3.2 Schema & Integrity Verification
Run comprehensive integrity, migration checksum, and permission checks:

```bash
python3 -m hermes.cli accounting-verify \
  --db-path "${REHEARSAL_DB_DIR}/isolated_ledger.db"
```

**Verification Checklist:**
- [x] SQLite `PRAGMA integrity_check` returns `ok`.
- [x] SQLite `PRAGMA foreign_key_check` returns zero violations.
- [x] Migration checksums in `schema_migrations` match SQL files in `hermes/accounting/migrations/`.
- [x] Database file POSIX permissions are strictly `0600` (owner read/write only).
- [x] Cutover record is in `PENDING` status.
- [x] Active reconciliation blockers count is 0.

---

## 4. Dry-Run Reconciliation Rehearsal on Archived Data (AC 2)

Reconciliation rehearsal exercises the complete ingestion, FIFO lot allocation, fee deduction, accounting snapshot computation, and policy evaluation lifecycle against historical trade records.

### 4.1 Rehearsal Command
Execute the dry-run rehearsal using the audited trade archive fixture:

```bash
python3 -m hermes.cli accounting-reconcile \
  --db-path "${REHEARSAL_DB_DIR}/rehearsal_trades.db" \
  --trades-file "/var/www/new-trading-system/fixtures/archived_trades_sample.json" \
  --dry-run
```

### 4.2 Rehearsal Guarantees
- **Zero Network Mutations:** No HTTP POST/PUT/DELETE requests or exchange mutation endpoints are invoked.
- **Zero Orders:** Evaluates rotation policies with `order_mutations_executed: 0` in shadow mode.
- **Zero Transfers:** Evaluates distribution policy; fails closed to `BLOCKED` with reason code `CUTOVER_NOT_APPROVED` because the genesis cutover is `PENDING`.
- **Zero Production Service Restarts:** Runs entirely within the CLI runner process without signaling systemd or PM2.

### 4.3 Expected Rehearsal Output Summary
```json
{
  "status": "REHEARSAL_SUCCESS",
  "dry_run": true,
  "input_trades_count": 9,
  "inserted_fills": 9,
  "duplicate_fills": 0,
  "quarantined_fills": 0,
  "sell_outcomes_count": 4,
  "sell_outcomes": [
    {
      "outcome_id": "out_...",
      "symbol": "BTCUSDT",
      "sell_trade_id": "1002",
      "sold_base_qty": "0.001",
      "gross_proceeds_usdt": "55",
      "fifo_cost_usdt": "50",
      "buy_fee_usdt": "0.05",
      "sell_fee_usdt": "0.055",
      "net_pnl_usdt": "4.895",
      "completeness": "VERIFIED"
    },
    {
      "outcome_id": "out_...",
      "symbol": "SOLUSDT",
      "sell_trade_id": "2003",
      "sold_base_qty": "1.5",
      "gross_proceeds_usdt": "180",
      "fifo_cost_usdt": "155",
      "buy_fee_usdt": "0.155",
      "sell_fee_usdt": "0.18",
      "net_pnl_usdt": "24.665",
      "completeness": "VERIFIED"
    },
    {
      "outcome_id": "out_...",
      "symbol": "ETHUSDT",
      "sell_trade_id": "3002",
      "sold_base_qty": "0.1",
      "gross_proceeds_usdt": "285",
      "fifo_cost_usdt": "300",
      "buy_fee_usdt": "0.3",
      "sell_fee_usdt": "0.285",
      "net_pnl_usdt": "-15.585",
      "completeness": "VERIFIED"
    },
    {
      "outcome_id": "out_...",
      "symbol": "PEPEUSDT",
      "sell_trade_id": "4002",
      "sold_base_qty": "10000000",
      "gross_proceeds_usdt": "92",
      "fifo_cost_usdt": "80",
      "buy_fee_usdt": "0.08",
      "sell_fee_usdt": "0.092",
      "net_pnl_usdt": "11.828",
      "completeness": "VERIFIED"
    }
  ],
  "accounting_snapshot": {
    "cutover_status": "PENDING",
    "cumulative_verified_net_pnl_usdt": "25.803",
    "cumulative_confirmed_distributions_usdt": "0",
    "distribution_surplus_usdt": "25.803",
    "completeness": "PARTIAL",
    "reconciliation_status": "PENDING",
    "unresolved_reason_codes": ["CUTOVER_NOT_APPROVED"]
  },
  "distribution_policy_evaluation": {
    "action": "BLOCKED",
    "target_amount_usdt": null,
    "reason_codes": [
      "CUTOVER_NOT_APPROVED",
      "ACCOUNTING_INCOMPLETE",
      "RECONCILIATION_REQUIRED"
    ]
  },
  "rotation_policy_evaluation": {
    "action": "APPROVE",
    "shadow_mode": true,
    "score_delta": "4.0",
    "order_mutations_executed": 0
  }
}
```

---

## 5. Automated Pre-Cutover Backup & Safe Restore Procedures (AC 3)

Prior to any production configuration change or cutover activation, an atomic online backup of both the SQLite database and legacy JSON state files MUST be captured.

### 5.1 Creating an Atomic Online Backup
```bash
# Define backup storage directory
BACKUP_DIR="/var/www/new-trading-system/backups/accounting"
mkdir -p "${BACKUP_DIR}"
chmod 0700 "${BACKUP_DIR}"

# Execute atomic backup
python3 -m hermes.cli accounting-backup \
  --db-path "/var/www/new-trading-system/hermes_ledger.db" \
  --backup-dir "${BACKUP_DIR}"
```

**Backup Characteristics:**
1. Uses `sqlite3.backup` API: copies data page-by-page while WAL journal is active without blocking concurrent queries.
2. Captures legacy state files: `daily_profit_state.json` and `hermes_trader_state.json`.
3. Sets `0600` permissions on all created backup artifacts.
4. Generates SHA-256 checksums recorded in the output.

### 5.2 Safe Restoration Procedure
If restoration is required during drill or recovery:

```bash
# Restore from verified backup file
python3 -m hermes.cli accounting-restore \
  --backup-file "${BACKUP_DIR}/hermes_ledger.db.bak.<timestamp>" \
  --db-path "/var/www/new-trading-system/hermes_ledger.db"
```

**Restoration Safety Steps:**
1. **Preflight Integrity Check:** Inspects the backup file with `PRAGMA integrity_check` before modifying the destination.
2. **Atomic Swap:** Writes the restored data to an isolated temporary file (`*.restore_tmp_*`), verifies its schema integrity, then replaces the target database via atomic rename (`os.replace`).
3. **Companion File Cleanup:** Unlinks any stale `-wal` and `-shm` companion files from prior sessions.
4. **Post-Restore Permission Enforcement:** Re-applies `0600` permissions and outputs the new SHA-256 hash.

---

## 6. Immediate Rollback Triggers & Safe Recovery Protocol (AC 4)

### 6.1 Rollback Trigger Matrix
An immediate emergency rollback to **Legacy Safe Mode** is triggered upon encountering any of the following operational conditions:

| Trigger ID | Condition / Threshold | Detection Metric / Log Pattern | Immediate Action |
| :--- | :--- | :--- | :--- |
| **RT-01** | FIFO Lot Reconciliation Mismatch | `PARTIAL` or `UNRESOLVED` realized outcome, missing base lot for SELL fill | Abort distribution, freeze ledger, execute rollback |
| **RT-02** | Trade Ingestion Sync Gap / Pagination Loop | Pagination reaching `max_pages=100` without completion or non-advancing `fromId` | Disarm accounting ingestion sync, revert to legacy safe mode |
| **RT-03** | Unvalued Fee Asset / Missing Valuation | `commission_usdt is None` or `UNVALUED` on non-USDT fee asset | Halt collector sweep, retain surplus, do not transfer |
| **RT-04** | Unexpected Negative Surplus / Balance Drift | Spot USDT free balance < 0 or negative surplus after positive closed trades | Trigger circuit breaker, stop collector |
| **RT-05** | Ambiguous Transfer Intent State | Transfer status `UNKNOWN` or network timeout during `/sapi/v1/asset/transfer` | Fail closed to reconciliation state; do NOT issue duplicate transfer |
| **RT-06** | Shadow Rotation Mutation Breach | Any live order placement attempted while `ROTATION_SHADOW_MODE=True` | Immediate daemon halt, revoke trade credentials |

### 6.2 Emergency Rollback Step-by-Step Execution
When a rollback trigger is met:

#### Step 1: Disarm All Phase 2 Feature Flags
Ensure the environment and configuration disable accounting collection and rotation:
```bash
# In .env or systemd environment overrides:
DAILY_PROFIT_COLLECTION=False
ACCOUNTING_ENABLED=False
ROTATION_ENABLED=False
ROTATION_SHADOW_MODE=True
AUTO_SWEEP_PROFIT_TO_FUNDING=False
```

#### Step 2: Restore Database from Pre-Cutover Backup
```bash
python3 -m hermes.cli accounting-restore \
  --backup-file "${BACKUP_DIR}/hermes_ledger.db.bak.<pre_cutover_timestamp>" \
  --db-path "/var/www/new-trading-system/hermes_ledger.db"
```

#### Step 3: Restore Legacy Safe State Files (if necessary)
```bash
cp "${BACKUP_DIR}/daily_profit_state.json.bak.<timestamp>" /var/www/new-trading-system/daily_profit_state.json
cp "${BACKUP_DIR}/hermes_trader_state.json.bak.<timestamp>" /var/www/new-trading-system/hermes_trader_state.json
chmod 0600 /var/www/new-trading-system/*.json
```

#### Step 4: Verify Database Health Post-Rollback
```bash
python3 -m hermes.cli accounting-verify \
  --db-path "/var/www/new-trading-system/hermes_ledger.db"
```

#### Step 5: Verify Legacy Safe Mode Operational State
```bash
python3 -m hermes.cli state
python3 -m hermes.cli check-positions
```

---

## 7. Status Declaration & Release Gate Checklist (AC 5)

### Explicit Status Declaration
```
================================================================================
STATUS: NOT DEPLOYED
This worktree and all artifacts are in staging / release-candidate mode.
NO automated service restarts, systemd service reloads, or cron jobs
have been scheduled or triggered.
================================================================================
```

### Pre-Cutover Release Gate Checklist (PO Sign-Off Required)

Before any live cutover or production service restart may occur:

- [x] **AC 1:** Isolated DB initialization and migration verification commands provided and verified.
- [x] **AC 2:** Dry-run reconciliation rehearsal on archived trade fixture executed successfully (0 orders, 0 transfers).
- [x] **AC 3:** Pre-cutover automated backup and restore commands defined and verified with atomic SQLite backup API.
- [x] **AC 4:** Immediate rollback trigger matrix and step-by-step recovery commands documented.
- [x] **AC 5:** Explicit `STATUS NOT DEPLOYED` stated; zero automated deployments or service restarts scheduled.
- [x] **Test Suite:** 100% of accounting and CLI ops tests passing (`Ran 6 tests ... OK`). Full project suite passing (183+ tests).
- [x] **Security Posture:** 0 Critical, 0 High security audit findings (`docs/security-accounting-and-rotation-audit.md`).
- [x] **File Permissions:** 0600 permissions enforced on database files, backups, and WAL companions.
- [ ] **PO Approval:** Explicit Product Owner authorization granted for live cutover schedule.
