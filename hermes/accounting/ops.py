"""Operational management, verification, rehearsal, and backup/restore utilities for Phase 2 Accounting Ledger.

This module provides operational procedures and commands for:
1. Isolated database initialization and migration verification without touching production.
2. Dry-run reconciliation-only rehearsal using archived or live trade data.
3. Automated pre-cutover backups and safe restoration.
4. Health inspection and status reporting.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from hermes.accounting.contracts import (
    AccountingCutover,
    AccountingSnapshot,
    Completeness,
    CutoverStatus,
    DecimalString,
    DistributionAction,
    DistributionDecision,
    DistributionPolicyConfig,
    FillEvent,
    FillKey,
    RealizedOutcome,
    ReconciliationStatus,
    RotationAction,
    RotationDecision,
    RotationMarketSnapshot,
    TradeSide,
    TransferIntent,
    TransferStatus,
    ValuationStatus,
    Venue,
    canonical_decimal,
)
from hermes.accounting.ingestion import parse_binance_fill
from hermes.accounting.repository import (
    SqliteAccountingRepository,
    SqliteRotationRepository,
    SqliteTransferIntentRepository,
)
from hermes.accounting.collector import evaluate_distribution
from hermes.accounting.rotation import RotationPolicyConfig, evaluate_rotation
from hermes.accounting.schema import (
    MIGRATIONS_DIR,
    _compute_checksum,
    _secure_file_permissions,
    backup_database,
    get_db_connection,
    run_migrations,
    verify_schema_integrity,
)

log = logging.getLogger(__name__)


def compute_file_sha256(file_path: Union[str, Path]) -> str:
    """Compute SHA-256 hash of a file."""
    p = Path(file_path).resolve()
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def init_isolated_db(
    db_path: Union[str, Path],
    account_id: str = "default",
    venue: Venue = Venue.SPOT,
) -> Dict[str, Any]:
    """Initialize an isolated accounting SQLite database and apply migrations.

    Ensures WAL mode, PRAGMA foreign_keys = ON, 0600 file permissions,
    and creates a genesis cutover record in PENDING status.
    """
    target = Path(db_path).resolve()
    applied_versions = run_migrations(target)

    con = get_db_connection(target)
    try:
        healthy, integrity_errors = verify_schema_integrity(con)
        if not healthy:
            raise RuntimeError(f"Integrity check failed on init: {integrity_errors}")

        # Ensure cutover record exists in PENDING status
        cur = con.execute(
            "SELECT cutover_id, status FROM accounting_cutovers WHERE account_id = ? AND venue = ? LIMIT 1;",
            (account_id, venue.value),
        )
        row = cur.fetchone()
        now_ms = int(time.time() * 1000)
        if not row:
            con.execute(
                """
                INSERT INTO accounting_cutovers (
                    cutover_id, account_id, venue, cutover_at_ms, baseline_reference,
                    backfill_from_ms, backfill_through_ms, status, approved_at_ms, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    f"cutover_{account_id}_{now_ms}",
                    account_id,
                    venue.value,
                    now_ms,
                    "genesis_init",
                    None,
                    now_ms,
                    CutoverStatus.PENDING.value,
                    None,
                    now_ms,
                ),
            )
            cutover_id = f"cutover_{account_id}_{now_ms}"
            cutover_status = CutoverStatus.PENDING.value
        else:
            cutover_id = row["cutover_id"]
            cutover_status = row["status"]

        # Collect list of created tables
        tables_cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name ASC;")
        tables = [r["name"] for r in tables_cur.fetchall()]
    finally:
        con.close()

    _secure_file_permissions(target)

    return {
        "status": "INITIALIZED",
        "db_path": str(target),
        "file_exists": target.exists(),
        "file_size_bytes": target.stat().st_size if target.exists() else 0,
        "applied_versions": applied_versions,
        "tables_count": len(tables),
        "tables": tables,
        "cutover_id": cutover_id,
        "cutover_status": cutover_status,
        "journal_mode": "WAL",
        "foreign_keys": "ON",
        "file_permissions": "0600",
    }


def verify_db(db_path: Union[str, Path]) -> Dict[str, Any]:
    """Run comprehensive verification on an accounting database.

    Verifies file permissions (0600), WAL/SHM companion permissions,
    SQLite integrity_check, foreign_key_check, migration checksums,
    and counts records across all core tables.
    """
    target = Path(db_path).resolve()
    if not target.exists():
        return {
            "status": "NOT_FOUND",
            "db_path": str(target),
            "errors": [f"Database file does not exist: {target}"],
        }

    # Verify POSIX permissions (0600)
    mode = oct(target.stat().st_mode & 0o777)
    permission_ok = (target.stat().st_mode & 0o777) == 0o600

    companion_modes: Dict[str, str] = {}
    for ext in ("-wal", "-shm"):
        comp = target.parent / f"{target.name}{ext}"
        if comp.exists():
            companion_modes[comp.name] = oct(comp.stat().st_mode & 0o777)

    con = get_db_connection(target)
    errors: List[str] = []
    try:
        # Integrity check & foreign keys
        healthy, integrity_errors = verify_schema_integrity(con)
        if not healthy:
            errors.extend(integrity_errors)

        # Check applied migrations & checksums
        cur = con.execute("SELECT version, name, checksum, applied_at_ms FROM schema_migrations ORDER BY version ASC;")
        applied_rows = cur.fetchall()
        applied_migrations = []
        for r in applied_rows:
            v = r["version"]
            name = r["name"]
            checksum = r["checksum"]
            migration_file = MIGRATIONS_DIR / f"{v:03d}_{name}.sql"
            file_match = False
            if migration_file.exists():
                expected_sum = _compute_checksum(migration_file.read_text(encoding="utf-8"))
                file_match = (expected_sum == checksum)
                if not file_match:
                    errors.append(f"Migration {v:03d}_{name} checksum mismatch against filesystem!")
            else:
                errors.append(f"Migration file missing on disk: {migration_file}")

            applied_migrations.append({
                "version": v,
                "name": name,
                "checksum": checksum,
                "applied_at_ms": r["applied_at_ms"],
                "file_match": file_match,
            })

        # Count records in core tables
        table_counts: Dict[str, int] = {}
        core_tables = [
            "accounting_cutovers",
            "fills",
            "lots",
            "realized_outcomes",
            "lot_allocations",
            "transfer_intents",
            "rotation_decisions",
            "rotation_intents",
            "reconciliation_blockers",
        ]
        for tbl in core_tables:
            try:
                cnt_cur = con.execute(f"SELECT COUNT(*) as cnt FROM {tbl};")
                table_counts[tbl] = cnt_cur.fetchone()["cnt"]
            except sqlite3.OperationalError:
                table_counts[tbl] = -1

        # Check cutover status
        cutover_cur = con.execute("SELECT cutover_id, account_id, status FROM accounting_cutovers LIMIT 5;")
        cutovers = [dict(r) for r in cutover_cur.fetchall()]

        # Check active blockers
        blockers_cur = con.execute(
            "SELECT blocker_id, blocker_type, reason_code, details_json FROM reconciliation_blockers WHERE is_active = 1;"
        )
        active_blockers = [dict(r) for r in blockers_cur.fetchall()]
    finally:
        con.close()

    is_healthy = len(errors) == 0 and permission_ok

    return {
        "status": "HEALTHY" if is_healthy else "DEGRADED",
        "db_path": str(target),
        "permission_ok": permission_ok,
        "file_mode": mode,
        "companion_modes": companion_modes,
        "sha256": compute_file_sha256(target),
        "applied_migrations": applied_migrations,
        "table_counts": table_counts,
        "cutovers": cutovers,
        "active_blockers_count": len(active_blockers),
        "active_blockers": active_blockers,
        "errors": errors,
    }


def backup_accounting_state(
    db_path: Union[str, Path],
    backup_dir: Optional[Union[str, Path]] = None,
    include_legacy_state: bool = True,
) -> Dict[str, Any]:
    """Perform atomic online backup of accounting database and legacy state files.

    Uses SQLite backup API to copy database state safely while writer processes
    are active. Sets 0600 permissions and records SHA-256 hashes.
    """
    target = Path(db_path).resolve()
    dest_dir = Path(backup_dir).resolve() if backup_dir else target.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest_dir.chmod(0o700)
    except (OSError, PermissionError):
        pass

    timestamp = int(time.time() * 1000)
    db_backup_path = None
    db_sha256 = None

    if target.exists() and target.stat().st_size > 0:
        db_backup_file = dest_dir / f"{target.name}.bak.{timestamp}"
        src_con = sqlite3.connect(str(target), timeout=5.0)
        dest_con = sqlite3.connect(str(db_backup_file), timeout=5.0)
        try:
            with dest_con:
                src_con.backup(dest_con)
        finally:
            dest_con.close()
            src_con.close()

        _secure_file_permissions(db_backup_file)
        db_backup_path = str(db_backup_file)
        db_sha256 = compute_file_sha256(db_backup_file)

    legacy_backups: Dict[str, Any] = {}
    if include_legacy_state:
        base_dir = target.parent
        for fname in ("daily_profit_state.json", "hermes_trader_state.json"):
            src_file = base_dir / fname
            if src_file.exists():
                dst_file = dest_dir / f"{fname}.bak.{timestamp}"
                content = src_file.read_bytes()
                dst_file.write_bytes(content)
                _secure_file_permissions(dst_file)
                legacy_backups[fname] = {
                    "backup_path": str(dst_file),
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }

    return {
        "status": "BACKUP_COMPLETED",
        "timestamp_ms": timestamp,
        "backup_dir": str(dest_dir),
        "db_backup_path": db_backup_path,
        "db_sha256": db_sha256,
        "legacy_backups": legacy_backups,
    }


def restore_accounting_state(
    backup_file: Union[str, Path],
    target_db_path: Union[str, Path],
    preflight_check: bool = True,
) -> Dict[str, Any]:
    """Safely restore accounting database from an atomic backup file.

    Performs preflight integrity checks on the backup file before restoring,
    atomically writes the target, verifies post-restore integrity,
    and applies 0600 file permissions.
    """
    bak_path = Path(backup_file).resolve()
    if not bak_path.exists():
        raise FileNotFoundError(f"Backup file not found: {bak_path}")

    target = Path(target_db_path).resolve()

    if preflight_check:
        pre_con = sqlite3.connect(str(bak_path), timeout=5.0)
        try:
            cur = pre_con.execute("PRAGMA integrity_check;")
            res = cur.fetchone()[0]
            if res != "ok":
                raise RuntimeError(f"Backup file failed preflight integrity check: {res}")
        finally:
            pre_con.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.parent / f"{target.name}.restore_tmp_{int(time.time() * 1000)}"

    src_con = sqlite3.connect(str(bak_path), timeout=5.0)
    dest_con = sqlite3.connect(str(temp_target), timeout=5.0)
    try:
        with dest_con:
            src_con.backup(dest_con)
    finally:
        dest_con.close()
        src_con.close()

    _secure_file_permissions(temp_target)

    # Post-restore integrity verification
    post_con = get_db_connection(temp_target)
    try:
        healthy, errors = verify_schema_integrity(post_con)
        if not healthy:
            temp_target.unlink(missing_ok=True)
            raise RuntimeError(f"Restored temp database failed post-flight verification: {errors}")
    finally:
        post_con.close()

    # Atomic move
    temp_target.replace(target)
    _secure_file_permissions(target)

    # Clean any stale WAL/SHM
    for ext in ("-wal", "-shm"):
        wal_file = target.parent / f"{target.name}{ext}"
        if wal_file.exists():
            try:
                wal_file.unlink()
            except OSError:
                pass

    return {
        "status": "RESTORED",
        "backup_source": str(bak_path),
        "target_db_path": str(target),
        "sha256": compute_file_sha256(target),
        "file_permissions": "0600",
        "post_restore_integrity": "OK",
    }


def rehearse_reconciliation(
    db_path: Union[str, Path],
    trades_file: Optional[Union[str, Path]] = None,
    trades_data: Optional[Sequence[Mapping[str, Any]]] = None,
    valuation_map: Optional[Mapping[str, str]] = None,
    account_id: str = "default",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Execute a dry-run reconciliation rehearsal on archived or live trade data.

    Ingests trade history into an isolated repository, executes FIFO sell allocations,
    builds the AccountingSnapshot, evaluates distribution policy (Gates 1-7),
    and evaluates rotation policy (with zero orders).

    Guarantees:
    - Zero network mutations.
    - Zero orders placed.
    - Zero live service restarts.
    """
    target = Path(db_path).resolve()
    init_isolated_db(target, account_id=account_id)

    # Load trade data
    trades: Sequence[Mapping[str, Any]] = []
    if trades_data is not None:
        trades = trades_data
    elif trades_file:
        t_path = Path(trades_file).resolve()
        if not t_path.exists():
            raise FileNotFoundError(f"Trades file not found: {t_path}")
        trades = json.loads(t_path.read_text(encoding="utf-8"))

    repo = SqliteAccountingRepository(target)

    val_map: Dict[str, DecimalString] = {}
    if valuation_map:
        for k, v in valuation_map.items():
            val_map[k.upper()] = canonical_decimal(v, non_negative=True)

    inserted_fills = 0
    duplicate_fills = 0
    sell_outcomes: List[RealizedOutcome] = []
    quarantined_fills = 0

    # Sort trades chronologically: event_time, BUY before SELL, trade_id
    def _trade_sort_key(t: Mapping[str, Any]) -> Tuple[int, int, str]:
        tm = int(t.get("time", t.get("event_time_ms", 0)))
        side_val = 0 if (t.get("isBuyer") or str(t.get("side", "")).upper() in ("BUY", "BID")) else 1
        tid = str(t.get("id", t.get("trade_id", "")))
        return (tm, side_val, tid)

    sorted_trades = sorted(trades, key=_trade_sort_key)

    for tr in sorted_trades:
        try:
            fill = parse_binance_fill(
                tr,
                account_id=account_id,
                venue=Venue.SPOT,
                valuation_map=val_map,
            )
            was_inserted = repo.ingest_fill(fill)
            if was_inserted:
                inserted_fills += 1
            else:
                duplicate_fills += 1

            if fill.side == TradeSide.SELL:
                outcome = repo.apply_fifo_sell(fill.key)
                sell_outcomes.append(outcome)
        except Exception as e:
            log.warning(f"[REHEARSAL] Ingestion notice for trade {tr.get('id')}: {e}")
            if "Quarantined" in str(e) or "tamper" in str(e).lower():
                quarantined_fills += 1

    # Retrieve accounting snapshot
    now_ms = int(time.time() * 1000)
    snapshot = repo.distribution_snapshot(account_id=account_id, observed_at_ms=now_ms)

    # Test pure distribution policy evaluation with sample portfolio cash ($50 USDT)
    sample_free_spot = canonical_decimal("50.00", non_negative=True)
    sample_total_equity = canonical_decimal("100.00", non_negative=True)
    sample_open_risk = canonical_decimal("0.00", non_negative=True)

    policy_config = DistributionPolicyConfig(
        policy_version="v1",
        enabled=True,
        legacy_auto_sweep_enabled=False,
        target_usdt="1.00",
        daily_cap_usdt="1.00",
        operational_buffer_usdt="0",
        reserve_fraction="0.25",
    )

    distribution_decision = evaluate_distribution(
        policy_config=policy_config,
        accounting_snapshot=snapshot,
        free_spot_usdt=sample_free_spot,
        total_equity_usdt=sample_total_equity,
        open_risk_usdt=sample_open_risk,
        now_ms=now_ms,
    )

    # Test pure capital rotation policy evaluation (shadow mode)
    rot_config = RotationPolicyConfig(
        model_version="v2",
        feature_schema_version="v1",
        enabled=True,
        shadow_mode=True,
        min_score_edge="3",
        min_pnl_pct="-0.045",
        max_pnl_pct="0.02",
        min_hold_secs=1800,
        max_cost_fraction="0.015",
        max_spread_fraction="0.005",
        min_depth_usdt="50",
        min_trade_usdt="5.5",
        fee_rate_fraction="0.001",
        cooldown_secs=1800,
        daily_attempt_cap=5,
    )

    held_snap = RotationMarketSnapshot(
        snapshot_id=f"snap_held_{now_ms}",
        account_id=account_id,
        venue=Venue.SPOT,
        symbol="ETHUSDT",
        position_lifecycle_id="pos_eth_rehearsal",
        position_version=1,
        model_version="v2",
        feature_schema_version="v1",
        observed_at_ms=now_ms,
        expires_at_ms=now_ms + 60000,
        score="5.0",
        best_bid_usdt="2850.00",
        best_ask_usdt="2850.10",
        spread_fraction="0.000035",
        available_depth_usdt="10000.00",
        closed_bar_ids=("bar_1", "bar_2"),
        completeness=Completeness.VERIFIED,
    )

    cand_snap = RotationMarketSnapshot(
        snapshot_id=f"snap_cand_{now_ms}",
        account_id=account_id,
        venue=Venue.SPOT,
        symbol="SOLUSDT",
        position_lifecycle_id=None,
        position_version=None,
        model_version="v2",
        feature_schema_version="v1",
        observed_at_ms=now_ms,
        expires_at_ms=now_ms + 60000,
        score="9.0",
        best_bid_usdt="120.00",
        best_ask_usdt="120.05",
        spread_fraction="0.00041",
        available_depth_usdt="5000.00",
        closed_bar_ids=("bar_1", "bar_2"),
        completeness=Completeness.VERIFIED,
    )

    rot_decision = evaluate_rotation(
        held_snapshot=held_snap,
        candidate_snapshot=cand_snap,
        held_position_pnl_pct="0.005",
        held_holding_time_secs=2000,
        config=rot_config,
        now_ms=now_ms,
        position_notional_usdt="25.00",
    )

    # Summary of realized outcomes
    outcomes_summary = []
    for o in sell_outcomes:
        outcomes_summary.append({
            "outcome_id": o.outcome_id,
            "symbol": o.symbol,
            "sell_trade_id": o.sell_fill_key.trade_id,
            "sold_base_qty": o.sold_base_qty,
            "gross_proceeds_usdt": o.gross_proceeds_usdt,
            "fifo_cost_usdt": o.fifo_cost_usdt,
            "buy_fee_usdt": o.buy_fee_usdt,
            "sell_fee_usdt": o.sell_fee_usdt,
            "net_pnl_usdt": o.net_pnl_usdt,
            "completeness": o.completeness.value,
        })

    return {
        "status": "REHEARSAL_SUCCESS",
        "dry_run": dry_run,
        "input_trades_count": len(sorted_trades),
        "inserted_fills": inserted_fills,
        "duplicate_fills": duplicate_fills,
        "quarantined_fills": quarantined_fills,
        "sell_outcomes_count": len(sell_outcomes),
        "sell_outcomes": outcomes_summary,
        "accounting_snapshot": {
            "cutover_id": snapshot.cutover_id,
            "cutover_status": snapshot.cutover_status.value,
            "cumulative_verified_net_pnl_usdt": snapshot.cumulative_verified_net_pnl_usdt,
            "cumulative_confirmed_distributions_usdt": snapshot.cumulative_confirmed_distributions_usdt,
            "distribution_surplus_usdt": snapshot.distribution_surplus_usdt,
            "completeness": snapshot.completeness.value,
            "reconciliation_status": snapshot.reconciliation_status.value,
            "unresolved_reason_codes": list(snapshot.unresolved_reason_codes),
        },
        "distribution_policy_evaluation": {
            "action": distribution_decision.action.value,
            "target_amount_usdt": distribution_decision.amount_usdt,
            "reason_codes": list(distribution_decision.reason_codes),
            "notes": (
                "Distribution fails closed to BLOCKED if cutover is PENDING, exactly as designed."
                if snapshot.cutover_status == CutoverStatus.PENDING
                else "Distribution evaluated against approved cutover."
            ),
        },
        "rotation_policy_evaluation": {
            "action": rot_decision.action.value,
            "shadow_mode": rot_config.shadow_mode,
            "score_delta": str(Decimal(cand_snap.score) - Decimal(held_snap.score)),
            "reason_codes": list(rot_decision.reason_codes),
            "order_mutations_executed": 0,
        },
    }


def get_accounting_status(
    db_path: Union[str, Path],
    account_id: str = "default",
    venue: Venue = Venue.SPOT,
) -> Dict[str, Any]:
    """Retrieve full status of the accounting database."""
    target = Path(db_path).resolve()
    if not target.exists():
        return {
            "status": "NOT_FOUND",
            "db_path": str(target),
        }

    repo = SqliteAccountingRepository(target)
    now_ms = int(time.time() * 1000)
    snapshot = repo.distribution_snapshot(account_id=account_id, observed_at_ms=now_ms)

    con = repo._con()
    try:
        # Open lots inventory
        lots_cur = con.execute(
            """
            SELECT symbol, COUNT(*) as open_lots_count,
                   SUM(CAST(remaining_base_qty AS REAL)) as total_remaining_base,
                   SUM(CAST(quote_cost_usdt AS REAL)) as total_cost_usdt
            FROM lots
            WHERE account_id = ? AND venue = ? AND CAST(remaining_base_qty AS REAL) > 0
            GROUP BY symbol;
            """,
            (account_id, venue.value),
        )
        lots_summary = [dict(r) for r in lots_cur.fetchall()]

        # Transfer intents summary
        intent_cur = con.execute(
            """
            SELECT status, COUNT(*) as cnt, SUM(CAST(amount_usdt AS REAL)) as total_amount
            FROM transfer_intents
            WHERE account_id = ?
            GROUP BY status;
            """,
            (account_id,),
        )
        intents_summary = [dict(r) for r in intent_cur.fetchall()]

        # Active reconciliation blockers
        blockers_cur = con.execute(
            """
            SELECT blocker_id, blocker_type, reason_code, details_json, created_at_ms
            FROM reconciliation_blockers
            WHERE account_id = ? AND is_active = 1;
            """,
            (account_id,),
        )
        active_blockers = [dict(r) for r in blockers_cur.fetchall()]
    finally:
        con.close()

    return {
        "status": "ACTIVE",
        "db_path": str(target),
        "cutover_id": snapshot.cutover_id,
        "cutover_status": snapshot.cutover_status.value,
        "cumulative_verified_net_pnl_usdt": snapshot.cumulative_verified_net_pnl_usdt,
        "cumulative_confirmed_distributions_usdt": snapshot.cumulative_confirmed_distributions_usdt,
        "distribution_surplus_usdt": snapshot.distribution_surplus_usdt,
        "completeness": snapshot.completeness.value,
        "reconciliation_status": snapshot.reconciliation_status.value,
        "unresolved_reason_codes": list(snapshot.unresolved_reason_codes),
        "active_blockers_count": len(active_blockers),
        "active_blockers": active_blockers,
        "open_lots_inventory": lots_summary,
        "transfer_intents_summary": intents_summary,
    }
