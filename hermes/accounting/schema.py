"""SQLite schema and migration management for Phase 2 durable accounting ledger.

Enforces:
- WAL mode, foreign keys ON, bounded busy timeout.
- Secure file permissions (0600).
- Atomic pre-migration backup.
- Non-destructive migration application, idempotent re-run, and rollback.
- Full integrity and foreign key checks.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import List, Optional, Tuple, Union

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class SchemaMigrationError(RuntimeError):
    """Raised when a schema migration or verification fails."""
    pass


class MigrationChecksumMismatchError(SchemaMigrationError):
    """Raised when an applied migration file checksum does not match stored record."""
    pass


def _secure_file_permissions(path: Path) -> None:
    """Ensure file and SQLite companion files have 0600 permissions."""
    try:
        if path.exists() and path.is_file():
            path.chmod(0o600)
        # Check potential WAL / SHM companions
        for ext in ("-wal", "-shm"):
            comp = path.parent / f"{path.name}{ext}"
            if comp.exists() and comp.is_file():
                comp.chmod(0o600)
    except (OSError, PermissionError):
        # Ignore on unsupported filesystems/platforms
        pass


def get_db_connection(
    db_path: Union[str, Path],
    *,
    timeout: float = 5.0,
    create_dirs: bool = True,
) -> sqlite3.Connection:
    """Create and configure a SQLite connection with WAL mode and foreign keys enabled.

    Args:
        db_path: Path to SQLite database file or ':memory:'.
        timeout: Bounded busy timeout in seconds.
        create_dirs: Whether to create parent directories.

    Returns:
        Configured sqlite3.Connection with row_factory=sqlite3.Row.
    """
    path_str = str(db_path)
    is_memory = path_str == ":memory:" or path_str.startswith("file::memory:")

    if not is_memory:
        p = Path(path_str).resolve()
        if create_dirs:
            p.parent.mkdir(parents=True, exist_ok=True)
        # If file does not exist, create it with 0600 permissions
        if not p.exists():
            p.touch(mode=0o600, exist_ok=True)
        else:
            _secure_file_permissions(p)

    con = sqlite3.connect(
        path_str,
        timeout=timeout,
        isolation_level=None,  # Autocommit mode by default; explicit transactions with BEGIN
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    con.row_factory = sqlite3.Row

    # Enforce SQLite durability and safety pragmas
    busy_timeout_ms = int(timeout * 1000)
    con.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")
    con.execute("PRAGMA foreign_keys = ON;")
    if not is_memory:
        con.execute("PRAGMA journal_mode = WAL;")
        con.execute("PRAGMA synchronous = NORMAL;")

    if not is_memory:
        _secure_file_permissions(Path(path_str).resolve())

    return con


def backup_database(
    db_path: Union[str, Path],
    backup_dir: Optional[Union[str, Path]] = None,
) -> Optional[Path]:
    """Create an atomic backup of the database prior to applying migrations.

    Returns:
        Path to the newly created backup file, or None if the database does not exist yet.
    """
    path_str = str(db_path)
    if path_str == ":memory:" or path_str.startswith("file::memory:"):
        return None

    src_path = Path(path_str).resolve()
    if not src_path.exists() or src_path.stat().st_size == 0:
        return None

    target_dir = Path(backup_dir).resolve() if backup_dir else src_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time() * 1000)
    backup_file = target_dir / f"{src_path.name}.bak.{timestamp}"

    src_con = sqlite3.connect(str(src_path), timeout=5.0)
    dest_con = sqlite3.connect(str(backup_file), timeout=5.0)
    try:
        with dest_con:
            src_con.backup(dest_con)
    finally:
        dest_con.close()
        src_con.close()

    _secure_file_permissions(backup_file)
    return backup_file


def _compute_checksum(content: str) -> str:
    """Compute deterministic SHA256 checksum for a migration script."""
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def verify_schema_integrity(con: sqlite3.Connection) -> Tuple[bool, List[str]]:
    """Run SQLite integrity and foreign key checks.

    Returns:
        Tuple of (is_healthy: bool, errors: List[str]).
    """
    errors: List[str] = []

    # PRAGMA integrity_check
    cur = con.execute("PRAGMA integrity_check;")
    rows = cur.fetchall()
    for row in rows:
        val = row[0] if isinstance(row, (tuple, sqlite3.Row)) else row["integrity_check"]
        if val != "ok":
            errors.append(f"integrity_check: {val}")

    # PRAGMA foreign_key_check
    cur = con.execute("PRAGMA foreign_key_check;")
    fk_rows = cur.fetchall()
    for r in fk_rows:
        errors.append(
            f"foreign_key_check failure in table '{r[0]}', rowid {r[1]}, target '{r[2]}', fkid {r[3]}"
        )

    return (len(errors) == 0, errors)


def run_migrations(
    db_path: Union[str, Path],
    target_version: Optional[int] = None,
    migrations_dir: Optional[Union[str, Path]] = None,
) -> List[int]:
    """Apply unapplied migrations up to target_version transactionally.

    Args:
        db_path: Database path or ':memory:'.
        target_version: Target schema version. If None, applies all available migrations.
        migrations_dir: Optional override for migrations directory.

    Returns:
        List of newly applied migration versions.
    """
    mdir = Path(migrations_dir).resolve() if migrations_dir else MIGRATIONS_DIR
    if not mdir.exists() or not mdir.is_dir():
        raise SchemaMigrationError(f"Migrations directory does not exist: {mdir}")

    # Discover migration files (e.g. 001_initial.sql)
    pattern = re.compile(r"^(\d+)_([a-zA-Z0-9_-]+)\.sql$")
    migration_files: List[Tuple[int, str, Path]] = []
    for f in mdir.glob("*.sql"):
        if f.name.endswith("_down.sql"):
            continue
        m = pattern.match(f.name)
        if m:
            v = int(m.group(1))
            name = m.group(2)
            migration_files.append((v, name, f))

    migration_files.sort(key=lambda x: x[0])

    con = get_db_connection(db_path)
    try:
        # Ensure schema_migrations table exists
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at_ms INTEGER NOT NULL,
                checksum TEXT NOT NULL
            );
            """
        )

        cur = con.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version ASC;")
        applied = {row["version"]: (row["name"], row["checksum"]) for row in cur.fetchall()}

        # Verify checksum of already applied migrations
        for v, name, f in migration_files:
            if v in applied:
                current_checksum = _compute_checksum(f.read_text(encoding="utf-8"))
                stored_name, stored_checksum = applied[v]
                if current_checksum != stored_checksum:
                    raise MigrationChecksumMismatchError(
                        f"Migration {v:03d}_{name} checksum mismatch! Stored: {stored_checksum}, current: {current_checksum}"
                    )

        # Filter pending migrations
        pending = [
            (v, name, f)
            for v, name, f in migration_files
            if v not in applied and (target_version is None or v <= target_version)
        ]

        if not pending:
            # Idempotent re-run: nothing to do
            return []

        # Take atomic backup before applying migrations
        backup_database(db_path)

        applied_versions: List[int] = []
        for v, name, f in pending:
            sql_content = f.read_text(encoding="utf-8")
            checksum = _compute_checksum(sql_content)
            now_ms = int(time.time() * 1000)

            # Apply migration script with tracking in a single atomic transaction
            con.execute("BEGIN IMMEDIATE;")
            try:
                # Execute migration statements
                # executescript issues COMMIT first if not careful, so execute parsed statements or wrapped script
                con.executescript(
                    f"{sql_content}\n"
                    f"INSERT INTO schema_migrations (version, name, applied_at_ms, checksum) "
                    f"VALUES ({v}, '{name}', {now_ms}, '{checksum}');"
                )
                applied_versions.append(v)
            except Exception as e:
                try:
                    con.execute("ROLLBACK;")
                except sqlite3.OperationalError:
                    pass
                raise SchemaMigrationError(f"Failed to apply migration {v:03d}_{name}: {e}") from e

        # Post-migration integrity verification
        healthy, errors = verify_schema_integrity(con)
        if not healthy:
            raise SchemaMigrationError(f"Schema integrity check failed after migrations: {errors}")

        return applied_versions
    finally:
        con.close()


def rollback_migrations(
    db_path: Union[str, Path],
    target_version: int = 0,
    migrations_dir: Optional[Union[str, Path]] = None,
) -> List[int]:
    """Rollback migrations down to target_version transactionally.

    Args:
        db_path: Database path or ':memory:'.
        target_version: Target schema version to rollback to.
        migrations_dir: Optional override for migrations directory.

    Returns:
        List of rolled back versions in execution order.
    """
    mdir = Path(migrations_dir).resolve() if migrations_dir else MIGRATIONS_DIR
    con = get_db_connection(db_path)
    try:
        cur = con.execute("SELECT version, name FROM schema_migrations ORDER BY version DESC;")
        applied_rows = cur.fetchall()
        to_rollback = [row for row in applied_rows if row["version"] > target_version]

        if not to_rollback:
            return []

        backup_database(db_path)

        rolled_back: List[int] = []
        for row in to_rollback:
            v = row["version"]
            name = row["name"]
            down_file = mdir / f"{v:03d}_{name}_down.sql"
            if not down_file.exists():
                raise SchemaMigrationError(
                    f"Rollback script not found for migration {v:03d}_{name}: {down_file}"
                )

            sql_content = down_file.read_text(encoding="utf-8")
            con.execute("BEGIN IMMEDIATE;")
            try:
                con.executescript(sql_content)
                check_table = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
                ).fetchone()
                if check_table:
                    con.execute("DELETE FROM schema_migrations WHERE version = ?;", (v,))
                rolled_back.append(v)
            except Exception as e:
                try:
                    con.execute("ROLLBACK;")
                except sqlite3.OperationalError:
                    pass
                raise SchemaMigrationError(f"Failed to rollback migration {v:03d}_{name}: {e}") from e

        # Run integrity check if database still has tables
        healthy, errors = verify_schema_integrity(con)
        if not healthy:
            raise SchemaMigrationError(f"Integrity check failed after rollback: {errors}")

        return rolled_back
    finally:
        con.close()


def init_db(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Convenience helper to initialize database and run all pending migrations.

    Returns:
        Open sqlite3.Connection.
    """
    if db_path is None:
        from hermes.config import ACCOUNTING_DB_PATH
        db_path = ACCOUNTING_DB_PATH

    run_migrations(db_path)
    return get_db_connection(db_path)
