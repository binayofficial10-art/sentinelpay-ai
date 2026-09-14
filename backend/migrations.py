"""Deterministic PostgreSQL migration runner and explicit legacy adoption."""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.database import DatabaseUnavailableError, close_database_pool, get_connection, using_sqlite


MIGRATIONS_DIRECTORY = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_PATTERN = re.compile(r"^(?P<version>\d{3})_(?P<name>(?!preflight_).+)\.sql$")
LEDGER_SCHEMA = "sentinelpay_meta"
LEDGER_TABLE = "schema_migrations"
ADVISORY_LOCK_KEY = 874213506
_OUTER_BEGIN = re.compile(r"(?im)^\s*BEGIN\s*;\s*$")
_OUTER_COMMIT = re.compile(r"(?im)^\s*COMMIT\s*;\s*$")


class MigrationError(RuntimeError):
    """Raised for deterministic migration contract failures."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str


def discover_migrations(directory: Path = MIGRATIONS_DIRECTORY) -> list[Migration]:
    """Return only supported versioned SQL migrations in deterministic order."""
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql"), key=lambda candidate: candidate.name):
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            continue
        found.append(
            Migration(
                version=int(match["version"]),
                name=path.name,
                path=path,
                checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    versions = [migration.version for migration in found]
    if len(versions) != len(set(versions)):
        raise MigrationError("Migration versions must be unique")
    return sorted(found, key=lambda migration: migration.version)


def _ensure_ledger(connection: Any) -> None:
    """Bootstrap metadata needed to atomically record migration 005 itself."""
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {LEDGER_SCHEMA}")
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {LEDGER_SCHEMA}.{LEDGER_TABLE} ("
        "version INTEGER PRIMARY KEY, migration_name TEXT NOT NULL UNIQUE, "
        "checksum CHAR(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'), "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )


def _ledger_exists(connection: Any) -> bool:
    row = connection.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"{LEDGER_SCHEMA}.{LEDGER_TABLE}",)
    ).fetchone()
    return bool(row and row[0])


def migration_status(connection: Any) -> dict[int, tuple[str, str]]:
    """Read ledger state without creating metadata or changing the database."""
    if not _ledger_exists(connection):
        return {}
    rows = connection.execute(
        f"SELECT version, migration_name, checksum FROM {LEDGER_SCHEMA}.{LEDGER_TABLE} ORDER BY version"
    ).fetchall()
    return {int(row[0]): (row[1], row[2]) for row in rows}


def _verify_checksums(applied: dict[int, tuple[str, str]], migrations: list[Migration]) -> None:
    by_version = {migration.version: migration for migration in migrations}
    for version, (name, checksum) in applied.items():
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(f"Applied migration {version} is absent from this source tree")
        if migration.name != name or migration.checksum != checksum:
            raise MigrationError(f"Checksum mismatch for already-applied migration {version}: {name}")


def schema_version(connection: Any, migrations: list[Migration] | None = None) -> dict[str, int | bool | None]:
    """Return safe version state; this function never creates a ledger."""
    if migrations is None:
        migrations = discover_migrations()
    applied = migration_status(connection)
    _verify_checksums(applied, migrations)
    expected = max((migration.version for migration in migrations), default=-1)
    current = max(applied, default=None)
    return {
        "current_version": current,
        "expected_latest_version": expected,
        "schema_current": current == expected and len(applied) == len(migrations),
    }


def _migration_sql(migration: Migration) -> str:
    """Remove only a script's outer transaction wrapper for runner-owned atomicity."""
    source = migration.path.read_text(encoding="utf-8")
    begins = list(_OUTER_BEGIN.finditer(source))
    commits = list(_OUTER_COMMIT.finditer(source))
    if not begins and not commits:
        return source
    if len(begins) != 1 or len(commits) != 1 or begins[0].start() > commits[0].start():
        raise MigrationError(f"Migration {migration.name} has an unsupported transaction wrapper")
    begin, commit = begins[0], commits[0]
    return source[: begin.start()] + source[begin.end() : commit.start()] + source[commit.end() :]


def _is_nontransactional(migration: Migration) -> bool:
    return bool(
        re.search(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY",
            migration.path.read_text(encoding="utf-8"),
            flags=re.IGNORECASE,
        )
    )


def _record_migration(connection: Any, migration: Migration) -> None:
    connection.execute(
        f"INSERT INTO {LEDGER_SCHEMA}.{LEDGER_TABLE} (version, migration_name, checksum) VALUES (%s, %s, %s)",
        (migration.version, migration.name, migration.checksum),
    )


def _apply_transactional_migration(connection: Any, migration: Migration) -> None:
    try:
        connection.execute(_migration_sql(migration))
        _record_migration(connection, migration)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _apply_nontransactional_migration(connection: Any, migration: Migration) -> None:
    """Run required autocommit DDL, then record it only after it succeeds."""
    connection.commit()
    previous_autocommit = connection.autocommit
    try:
        connection.autocommit = True
        connection.execute(migration.path.read_text(encoding="utf-8"))
    finally:
        connection.autocommit = previous_autocommit
    try:
        _record_migration(connection, migration)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _public_schema_exists(connection: Any) -> bool:
    row = connection.execute("SELECT to_regclass('public.transactions') IS NOT NULL").fetchone()
    return bool(row and row[0])


def apply_migrations() -> list[int]:
    """Apply outstanding PostgreSQL migrations without inferring legacy state."""
    if using_sqlite():
        raise MigrationError("PostgreSQL migrations are not applicable to SQLite")
    migrations = discover_migrations()
    applied_versions: list[int] = []
    with get_connection() as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            ledger_existed = _ledger_exists(connection)
            if not ledger_existed and _public_schema_exists(connection):
                raise MigrationError(
                    "Existing public schema has no migration ledger; use explicit adoption after preflight"
                )
            _ensure_ledger(connection)
            connection.commit()
            applied = migration_status(connection)
            _verify_checksums(applied, migrations)
            for migration in migrations:
                if migration.version in applied:
                    continue
                if _is_nontransactional(migration):
                    _apply_nontransactional_migration(connection, migration)
                else:
                    _apply_transactional_migration(connection, migration)
                applied_versions.append(migration.version)
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            connection.commit()
    return applied_versions


_REQUIRED_RELATIONS = (
    "users", "sessions", "transactions", "audit_events", "alerts", "rate_limit_buckets", "action_idempotency",
)
_REQUIRED_TRANSACTION_CONSTRAINTS = (
    "transactions_amount_minor_positive", "transactions_currency_format", "transactions_velocity_nonnegative",
    "transactions_risk_score_range", "transactions_risk_level_allowed", "transactions_decision_allowed",
    "transactions_review_decision_allowed", "transactions_analysis_source_allowed", "transactions_provider_allowed",
    "transactions_analysis_provider_match", "transactions_amount_positive", "transactions_amount_minor_consistent",
)


def _relation_exists(connection: Any, name: str) -> bool:
    row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return bool(row and row[0])


def _column_exists(connection: Any, table: str, column: str) -> bool:
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s)",
        (table, column),
    ).fetchone()
    return bool(row and row[0])


def _validated_constraint(connection: Any, name: str) -> bool:
    row = connection.execute(
        "SELECT convalidated FROM pg_constraint "
        "WHERE conrelid='public.transactions'::regclass AND conname=%s "
        "UNION ALL SELECT convalidated FROM pg_constraint "
        "WHERE conrelid='public.users'::regclass AND conname=%s",
        (name, name),
    ).fetchone()
    return bool(row and row[0])


def _verify_adoptable_schema(connection: Any) -> None:
    missing = [name for name in _REQUIRED_RELATIONS if not _relation_exists(connection, f"public.{name}")]
    if missing:
        raise MigrationError("Cannot adopt unsupported schema; missing " + ", ".join(missing))
    amount = connection.execute(
        "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='transactions' AND column_name='amount'"
    ).fetchone()
    if tuple(amount or ()) != (14, 2):
        raise MigrationError("Cannot adopt schema without canonical NUMERIC(14,2) transaction amount")
    missing_columns = [
        name for name in ("idempotency_key", "idempotency_request_hash", "amount_minor")
        if not _column_exists(connection, "transactions", name)
    ]
    if missing_columns:
        raise MigrationError("Cannot adopt schema; missing transaction columns " + ", ".join(missing_columns))
    if not _column_exists(connection, "users", "role"):
        raise MigrationError("Cannot adopt schema; missing users.role")
    missing_constraints = [name for name in _REQUIRED_TRANSACTION_CONSTRAINTS if not _validated_constraint(connection, name)]
    if missing_constraints:
        raise MigrationError("Cannot adopt schema; unvalidated constraints " + ", ".join(missing_constraints))
    if not _validated_constraint(connection, "users_role_allowed"):
        raise MigrationError("Cannot adopt schema; users role constraint is absent or unvalidated")
    if not _relation_exists(connection, "public.idx_transactions_user_idempotency"):
        raise MigrationError("Cannot adopt schema; idempotency index is absent")


def adopt_existing_schema() -> list[int]:
    """Explicitly verify 000--004 state before recording immutable ledger rows."""
    if using_sqlite():
        raise MigrationError("PostgreSQL migration adoption is not applicable to SQLite")
    migrations = discover_migrations()
    with get_connection() as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            applied = migration_status(connection)
            _verify_checksums(applied, migrations)
            if applied:
                raise MigrationError("Migration ledger is already populated; adoption is not permitted")
            _verify_adoptable_schema(connection)
            try:
                _ensure_ledger(connection)
                for migration in migrations:
                    _record_migration(connection, migration)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            connection.commit()
    return [migration.version for migration in migrations]


def main() -> int:
    parser = argparse.ArgumentParser(description="SentinelPay PostgreSQL migration control")
    parser.add_argument("command", choices=("status", "apply", "adopt-existing"))
    arguments = parser.parse_args()
    try:
        if arguments.command == "status":
            with get_connection() as connection:
                status = schema_version(connection)
            print(
                "current_version=" + str(status["current_version"])
                + " expected_latest_version=" + str(status["expected_latest_version"])
                + " schema_current=" + str(status["schema_current"])
            )
        elif arguments.command == "apply":
            print("applied=" + ",".join(map(str, apply_migrations())))
        else:
            print("adopted=" + ",".join(map(str, adopt_existing_schema())))
    except (DatabaseUnavailableError, MigrationError) as error:
        print(f"migration_error={type(error).__name__}")
        return 1
    finally:
        close_database_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
