"""Persistent transaction storage for local SQLite and production PostgreSQL."""

import os
import json
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class DatabaseUnavailableError(RuntimeError):
    """Raised when persistent storage cannot be reached or configured."""


class ActionResourceNotFoundError(RuntimeError):
    """Raised inside an idempotent write transaction when the resource is absent."""


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOCAL_DATABASE_PATH = Path(__file__).resolve().parent / "sentinelpay.db"

SQLITE_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'analyst', 'admin')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'analyst', 'admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SQLITE_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SQLITE_AUDIT_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    success INTEGER NOT NULL,
    source_hash TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_AUDIT_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    success BOOLEAN NOT NULL,
    source_hash TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SQLITE_ALERTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    severity TEXT NOT NULL CHECK (severity IN ('MEDIUM', 'HIGH', 'CRITICAL')),
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    assigned_analyst_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_ALERTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    transaction_id BIGINT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    severity TEXT NOT NULL CHECK (severity IN ('MEDIUM', 'HIGH', 'CRITICAL')),
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    assigned_analyst_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SQLITE_ACTION_IDEMPOTENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_idempotency (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    action_type TEXT NOT NULL,
    resource_id INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, idempotency_key)
)
"""

POSTGRES_ACTION_IDEMPOTENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_idempotency (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    action_type TEXT NOT NULL,
    resource_id BIGINT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, idempotency_key)
)
"""

SQLITE_RATE_LIMIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    scope TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    window_started_at TEXT NOT NULL,
    PRIMARY KEY (scope, subject_key)
)
"""

POSTGRES_RATE_LIMIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    scope TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    window_started_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (scope, subject_key)
)
"""

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL DEFAULT 'anonymous',
    amount TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0 AND amount_minor <= 99999999999999),
    idempotency_key TEXT,
    idempotency_request_hash TEXT,
    currency TEXT NOT NULL DEFAULT 'INR',
    merchant TEXT NOT NULL,
    sender TEXT NOT NULL,
    receiver TEXT NOT NULL,
    location TEXT NOT NULL,
    device TEXT NOT NULL,
    velocity INTEGER NOT NULL,
    transaction_timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    risk_score INTEGER NOT NULL,
    risk_level TEXT NOT NULL,
    decision TEXT NOT NULL,
    provider TEXT NOT NULL,
    explanation TEXT NOT NULL,
    ai_explanation TEXT NOT NULL,
    analysis_source TEXT NOT NULL,
    review_decision TEXT,
    reviewed_at TEXT,
    processing_time_ms REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL DEFAULT 'anonymous',
    amount NUMERIC(18, 2) NOT NULL,
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0 AND amount_minor <= 99999999999999),
    idempotency_key TEXT,
    idempotency_request_hash TEXT,
    currency TEXT NOT NULL DEFAULT 'INR',
    merchant TEXT NOT NULL,
    sender TEXT NOT NULL,
    receiver TEXT NOT NULL,
    location TEXT NOT NULL,
    device TEXT NOT NULL,
    velocity INTEGER NOT NULL,
    transaction_timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    risk_score INTEGER NOT NULL,
    risk_level TEXT NOT NULL,
    decision TEXT NOT NULL,
    provider TEXT NOT NULL,
    explanation TEXT NOT NULL,
    ai_explanation TEXT NOT NULL,
    analysis_source TEXT NOT NULL,
    review_decision TEXT,
    reviewed_at TIMESTAMPTZ,
    processing_time_ms DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

TRANSACTION_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_transactions_created_at
ON transactions (created_at DESC, id DESC)
"""

TRANSACTION_USER_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_transactions_user_created_at
ON transactions (user_id, created_at DESC, id DESC)
"""

TRANSACTION_IDEMPOTENCY_INDEX_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_user_idempotency
ON transactions (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL
"""

SESSION_TOKEN_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash
ON sessions (token_hash)
"""

AUDIT_USER_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_audit_events_user_created_at
ON audit_events (user_id, created_at DESC, id DESC)
"""

AUDIT_EVENT_TYPE_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_audit_events_type_created_at
ON audit_events (event_type, created_at DESC, id DESC)
"""

AUDIT_CREATED_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_audit_events_created_at
ON audit_events (created_at DESC, id DESC)
"""

ALERT_USER_STATUS_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_alerts_user_status_created_at
ON alerts (user_id, status, created_at DESC, id DESC)
"""

RATE_LIMIT_WINDOW_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_rate_limit_buckets_window_started_at
ON rate_limit_buckets (window_started_at)
"""

SQLITE_MIGRATION_COLUMNS = {
    "user_id": "INTEGER",
    "session_id": "TEXT NOT NULL DEFAULT 'anonymous'",
    "currency": "TEXT NOT NULL DEFAULT 'INR'",
    "merchant": "TEXT NOT NULL DEFAULT ''",
    "transaction_timestamp": "TEXT NOT NULL DEFAULT ''",
    "provider": "TEXT NOT NULL DEFAULT 'rule_based_fallback'",
    "explanation": "TEXT NOT NULL DEFAULT ''",
    "review_decision": "TEXT",
    "reviewed_at": "TEXT",
    "processing_time_ms": "REAL",
    "idempotency_key": "TEXT",
    "idempotency_request_hash": "TEXT",
    "amount_minor": "INTEGER",
}

SQLITE_USER_MIGRATION_COLUMNS = {"role": "TEXT NOT NULL DEFAULT 'viewer'"}
_schema_lock = threading.Lock()
_initialized_schema_keys: set[str] = set()


def using_sqlite() -> bool:
    return not DATABASE_URL or DATABASE_URL.startswith("sqlite:///")


def persistence_enabled() -> bool:
    """Use SQLite locally, but never write a SQLite file in Vercel functions."""
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
        return bool(DATABASE_URL) and not DATABASE_URL.startswith("sqlite:///")
    return True


def sqlite_path() -> str:
    if DATABASE_URL.startswith("sqlite:///"):
        return DATABASE_URL.removeprefix("sqlite:///")
    return str(LOCAL_DATABASE_PATH)


@contextmanager
def get_connection() -> Iterator[Any]:
    """Open a short-lived database connection suitable for Vercel functions."""
    if using_sqlite():
        try:
            connection = sqlite3.connect(sqlite_path())
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise DatabaseUnavailableError("Could not access the local SQLite database") from error
        return

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise DatabaseUnavailableError("PostgreSQL support is not installed") from error

    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
            yield connection
    except ActionResourceNotFoundError:
        # A domain result from an operation inside this context is not a
        # connectivity failure. Route handlers map it to a controlled 404.
        raise
    except psycopg.Error as error:
        raise DatabaseUnavailableError("Could not connect to DATABASE_URL") from error


def initialize_database(connection: Any) -> None:
    """Apply repeatable schema initialization for local SQLite or PostgreSQL."""
    connection.execute(SQLITE_USERS_SCHEMA if using_sqlite() else POSTGRES_USERS_SCHEMA)
    connection.execute(SQLITE_SESSIONS_SCHEMA if using_sqlite() else POSTGRES_SESSIONS_SCHEMA)
    connection.execute(SQLITE_AUDIT_EVENTS_SCHEMA if using_sqlite() else POSTGRES_AUDIT_EVENTS_SCHEMA)
    connection.execute(SQLITE_RATE_LIMIT_SCHEMA if using_sqlite() else POSTGRES_RATE_LIMIT_SCHEMA)
    connection.execute(SQLITE_SCHEMA if using_sqlite() else POSTGRES_SCHEMA)
    connection.execute(SQLITE_ALERTS_SCHEMA if using_sqlite() else POSTGRES_ALERTS_SCHEMA)
    connection.execute(SQLITE_ACTION_IDEMPOTENCY_SCHEMA if using_sqlite() else POSTGRES_ACTION_IDEMPOTENCY_SCHEMA)
    if using_sqlite():
        existing_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(transactions)").fetchall()
        }
        for column, definition in SQLITE_MIGRATION_COLUMNS.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE transactions ADD COLUMN {column} {definition}")
        existing_user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
        for column, definition in SQLITE_USER_MIGRATION_COLUMNS.items():
            if column not in existing_user_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        connection.execute("UPDATE transactions SET merchant = receiver WHERE merchant = ''")
        connection.execute(
            "UPDATE transactions SET provider = 'gemini' "
            "WHERE analysis_source = 'gemini' AND provider = 'rule_based_fallback'"
        )
        connection.execute("UPDATE transactions SET explanation = ai_explanation WHERE explanation = ''")
        connection.execute(
            "UPDATE transactions SET transaction_timestamp = created_at "
            "WHERE transaction_timestamp = ''"
        )
        # Legacy SQLite rows did not retain an exact minor-unit value. Validate
        # every legacy value with the same no-rounding rule as new writes; an
        # invalid historical value stops migration for manual remediation.
        for row in connection.execute(
            "SELECT id, amount FROM transactions WHERE amount_minor IS NULL"
        ).fetchall():
            connection.execute(
                "UPDATE transactions SET amount_minor = ? WHERE id = ?",
                (money_minor_units(row["amount"]), row["id"]),
            )
    # PostgreSQL upgrades are applied through versioned deployment migrations.
    # Request handlers must never run DDL against a live production database.
    connection.execute(TRANSACTION_INDEX_SCHEMA)
    connection.execute(TRANSACTION_USER_INDEX_SCHEMA)
    connection.execute(TRANSACTION_IDEMPOTENCY_INDEX_SCHEMA)
    connection.execute(SESSION_TOKEN_INDEX_SCHEMA)
    connection.execute(AUDIT_USER_INDEX_SCHEMA)
    connection.execute(AUDIT_EVENT_TYPE_INDEX_SCHEMA)
    connection.execute(AUDIT_CREATED_INDEX_SCHEMA)
    connection.execute(ALERT_USER_STATUS_INDEX_SCHEMA)
    connection.execute(RATE_LIMIT_WINDOW_INDEX_SCHEMA)


def ensure_schema(connection: Any) -> None:
    """Initialize local SQLite only; PostgreSQL DDL belongs exclusively to migrations."""
    if not using_sqlite():
        return
    schema_key = DATABASE_URL or str(LOCAL_DATABASE_PATH)
    if schema_key in _initialized_schema_keys:
        return
    with _schema_lock:
        if schema_key not in _initialized_schema_keys:
            initialize_database(connection)
            _initialized_schema_keys.add(schema_key)


def serialize_transaction(row: Any) -> dict[str, Any]:
    transaction = dict(row)
    if transaction.get("amount_minor") is not None:
        amount_minor = int(transaction["amount_minor"])
        transaction["amount"] = f"{amount_minor // 100}.{amount_minor % 100:02d}"
    for timestamp_column in ("transaction_timestamp", "created_at"):
        timestamp = transaction.get(timestamp_column)
        if hasattr(timestamp, "isoformat"):
            transaction[timestamp_column] = timestamp.isoformat()
    return transaction


def money_minor_units(value: Any) -> int:
    """Validate an exact decimal amount and convert it to the canonical cents integer."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Amount must be a valid decimal value") from error
    if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999999.99") or amount.as_tuple().exponent < -2:
        raise ValueError("Amount must be positive with at most two decimal places")
    return int(amount * 100)


def save_transaction(
    record: dict[str, Any],
    *,
    alert: dict[str, str] | None = None,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Save one assessment and return the stored record, including its database id."""
    if not persistence_enabled():
        return None

    explanation = record.get("explanation", record["ai_explanation"])
    columns = (
        "user_id", "session_id", "amount", "amount_minor", "idempotency_key", "idempotency_request_hash", "currency", "merchant", "sender", "receiver", "location",
        "device", "velocity", "transaction_timestamp", "risk_score", "risk_level", "decision",
        "provider", "explanation", "ai_explanation", "analysis_source", "processing_time_ms",
    )
    values_by_column = {
        "user_id": record["user_id"],
        "session_id": record.get("session_id", "anonymous"),
        "amount": record["amount"],
        "amount_minor": record.get("amount_minor", money_minor_units(record["amount"])),
        "idempotency_key": record.get("idempotency_key"),
        "idempotency_request_hash": record.get("idempotency_request_hash"),
        "currency": record.get("currency", "INR"),
        "merchant": record.get("merchant", record["receiver"]),
        "sender": record["sender"],
        "receiver": record["receiver"],
        "location": record["location"],
        "device": record["device"],
        "velocity": record["velocity"],
        "transaction_timestamp": record.get("transaction_timestamp") or datetime.now(timezone.utc),
        "risk_score": record["risk_score"],
        "risk_level": record["risk_level"],
        "decision": record["decision"],
        "provider": record.get("provider", "rule_based_fallback"),
        "explanation": explanation,
        "ai_explanation": record["ai_explanation"],
        "analysis_source": record["analysis_source"],
        "processing_time_ms": record.get("processing_time_ms"),
    }
    if using_sqlite():
        transaction_timestamp = values_by_column["transaction_timestamp"]
        if hasattr(transaction_timestamp, "isoformat"):
            values_by_column["transaction_timestamp"] = transaction_timestamp.isoformat()
    values = tuple(values_by_column[column] for column in columns)
    placeholders = ", ".join("?" if using_sqlite() else "%s" for _ in columns)
    query = (
        f"INSERT INTO transactions ({', '.join(columns)}) VALUES ({placeholders})"
        + (" ON CONFLICT(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING" if record.get("idempotency_key") else "")
        + " RETURNING id, user_id, session_id, amount, amount_minor, idempotency_key, idempotency_request_hash, currency, merchant, sender, receiver, location, device, "
        "velocity, transaction_timestamp, risk_score, risk_level, decision, provider, explanation, "
        "ai_explanation, analysis_source, processing_time_ms, review_decision, reviewed_at, created_at"
    )

    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, values).fetchone()
        if not row:
            return None
        stored = serialize_transaction(row)
        if alert:
            connection.execute(
                "INSERT INTO alerts (user_id, transaction_id, severity, title, reason) VALUES ("
                + ", ".join(["?" if using_sqlite() else "%s"] * 5) + ")",
                (record["user_id"], stored["id"], alert["severity"], alert["title"], alert["reason"]),
            )
        if audit:
            connection.execute(
                "INSERT INTO audit_events (event_type, user_id, success, source_hash, metadata_json) VALUES ("
                + ", ".join(["?" if using_sqlite() else "%s"] * 5) + ")",
                (
                    audit["event_type"], record["user_id"], True, audit.get("source_hash"),
                    json.dumps(audit.get("metadata", {}), sort_keys=True, separators=(",", ":")),
                ),
            )
        return stored


def get_recent_transactions(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent assessments newest first."""
    if not persistence_enabled():
        return []

    placeholder = "?" if using_sqlite() else "%s"
    query = (
        "SELECT id, user_id, session_id, amount, amount_minor, idempotency_key, idempotency_request_hash, currency, merchant, sender, receiver, location, device, velocity, "
        "transaction_timestamp, risk_score, risk_level, decision, provider, explanation, ai_explanation, "
        "analysis_source, processing_time_ms, review_decision, reviewed_at, created_at "
        f"FROM transactions WHERE user_id = {placeholder} ORDER BY created_at DESC, id DESC LIMIT {placeholder}"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        return [serialize_transaction(row) for row in connection.execute(query, (user_id, limit)).fetchall()]


def get_transaction(transaction_id: int, user_id: int) -> dict[str, Any] | None:
    """Return one persisted transaction, or None when it does not exist."""
    if not persistence_enabled():
        return None

    placeholder = "?" if using_sqlite() else "%s"
    query = (
        "SELECT id, user_id, session_id, amount, amount_minor, idempotency_key, idempotency_request_hash, currency, merchant, sender, receiver, location, device, velocity, "
        "transaction_timestamp, risk_score, risk_level, decision, provider, explanation, ai_explanation, "
        "analysis_source, processing_time_ms, review_decision, reviewed_at, created_at "
        f"FROM transactions WHERE id = {placeholder} AND user_id = {placeholder}"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, (transaction_id, user_id)).fetchone()
        return serialize_transaction(row) if row else None


def get_transaction_by_idempotency_key(idempotency_key: str, user_id: int) -> dict[str, Any] | None:
    """Return the original persisted result for an authenticated retry."""
    if not persistence_enabled():
        return None
    placeholder = "?" if using_sqlite() else "%s"
    query = (
        "SELECT id, user_id, session_id, amount, amount_minor, idempotency_key, idempotency_request_hash, currency, merchant, sender, receiver, location, device, velocity, "
        "transaction_timestamp, risk_score, risk_level, decision, provider, explanation, ai_explanation, analysis_source, processing_time_ms, review_decision, reviewed_at, created_at "
        f"FROM transactions WHERE user_id = {placeholder} AND idempotency_key = {placeholder}"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, (user_id, idempotency_key)).fetchone()
        return serialize_transaction(row) if row else None


def set_review_decision(transaction_id: int, user_id: int, decision: str) -> dict[str, Any] | None:
    """Persist an authenticated analyst decision; ownership is enforced in SQL."""
    if not persistence_enabled():
        return None
    placeholder = "?" if using_sqlite() else "%s"
    reviewed_at: Any = datetime.now(timezone.utc)
    if using_sqlite():
        reviewed_at = reviewed_at.isoformat()
    query = (
        "UPDATE transactions SET review_decision = " + placeholder + ", reviewed_at = " + placeholder
        + " WHERE id = " + placeholder + " AND user_id = " + placeholder
        + " RETURNING id, user_id, session_id, amount, amount_minor, currency, merchant, sender, receiver, location, device, velocity, "
        "transaction_timestamp, risk_score, risk_level, decision, provider, explanation, ai_explanation, analysis_source, processing_time_ms, review_decision, reviewed_at, created_at"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, (decision, reviewed_at, transaction_id, user_id)).fetchone()
        return serialize_transaction(row) if row else None


def consume_rate_limit(*, scope: str, subject_key: str, window_seconds: int) -> tuple[int, int]:
    """Atomically consume a shared fixed-window allowance and return count/retry seconds."""
    if not persistence_enabled():
        raise DatabaseUnavailableError("Rate limiting storage is unavailable")
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_seconds
    placeholder = "?" if using_sqlite() else "%s"
    now_value: Any = now.isoformat() if using_sqlite() else now
    cutoff_value: Any = datetime.fromtimestamp(cutoff, timezone.utc)
    if using_sqlite():
        cutoff_value = cutoff_value.isoformat()
    query = (
        "INSERT INTO rate_limit_buckets (scope, subject_key, request_count, window_started_at) "
        f"VALUES ({placeholder}, {placeholder}, 1, {placeholder}) "
        "ON CONFLICT(scope, subject_key) DO UPDATE SET "
        "request_count = CASE WHEN rate_limit_buckets.window_started_at <= " + placeholder
        + " THEN 1 ELSE rate_limit_buckets.request_count + 1 END, "
        "window_started_at = CASE WHEN rate_limit_buckets.window_started_at <= " + placeholder
        + " THEN excluded.window_started_at ELSE rate_limit_buckets.window_started_at END "
        "RETURNING request_count, window_started_at"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, (scope, subject_key, now_value, cutoff_value, cutoff_value)).fetchone()
    result = dict(row)
    started_at = result["window_started_at"]
    if isinstance(started_at, str):
        started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    retry_after = max(1, int((started_at.timestamp() + window_seconds) - now.timestamp()))
    return int(result["request_count"]), retry_after


def write_audit_event(
    *,
    event_type: str,
    success: bool,
    user_id: int | None,
    source_hash: str | None,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Store security telemetry without secrets, tokens, passwords, or transaction content."""
    if not persistence_enabled():
        raise DatabaseUnavailableError("Audit logging storage is unavailable")
    placeholder = "?" if using_sqlite() else "%s"
    metadata_value: Any = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    if not using_sqlite():
        metadata_value = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    query = (
        "INSERT INTO audit_events (event_type, user_id, success, source_hash, metadata_json) "
        f"VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}) "
        "RETURNING id, event_type, user_id, success, source_hash, metadata_json, created_at"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, (event_type, user_id, success, source_hash, metadata_value)).fetchone()
        return serialize_audit_event(row) if row else None


def serialize_audit_event(row: Any) -> dict[str, Any]:
    event = dict(row)
    timestamp = event.get("created_at")
    if hasattr(timestamp, "isoformat"):
        event["created_at"] = timestamp.isoformat()
    metadata = event.get("metadata_json")
    if isinstance(metadata, str):
        try:
            event["metadata"] = json.loads(metadata)
        except json.JSONDecodeError:
            event["metadata"] = {}
    else:
        event["metadata"] = metadata or {}
    event.pop("metadata_json", None)
    return event


def serialize_alert(row: Any) -> dict[str, Any]:
    alert = dict(row)
    for field in ("created_at", "updated_at"):
        if hasattr(alert.get(field), "isoformat"):
            alert[field] = alert[field].isoformat()
    return alert


def create_alert(*, user_id: int, transaction_id: int, severity: str, title: str, reason: str) -> dict[str, Any] | None:
    if not persistence_enabled():
        return None
    placeholder = "?" if using_sqlite() else "%s"
    query = ("INSERT INTO alerts (user_id, transaction_id, severity, title, reason) VALUES (" + ", ".join([placeholder] * 5) + ") "
        "RETURNING id, user_id, transaction_id, severity, title, reason, status, assigned_analyst_id, created_at, updated_at")
    with get_connection() as connection:
        ensure_schema(connection)
        return serialize_alert(connection.execute(query, (user_id, transaction_id, severity, title, reason)).fetchone())


def get_alerts_for_user(user_id: int, limit: int = 100) -> list[dict[str, Any]]:
    if not persistence_enabled(): return []
    placeholder = "?" if using_sqlite() else "%s"
    with get_connection() as connection:
        ensure_schema(connection)
        rows = connection.execute("SELECT id, user_id, transaction_id, severity, title, reason, status, assigned_analyst_id, created_at, updated_at FROM alerts WHERE user_id = " + placeholder + " ORDER BY created_at DESC, id DESC LIMIT " + placeholder, (user_id, limit)).fetchall()
    return [serialize_alert(row) for row in rows]


def update_alert_status(alert_id: int, user_id: int, status: str) -> dict[str, Any] | None:
    if not persistence_enabled(): return None
    placeholder = "?" if using_sqlite() else "%s"
    now: Any = datetime.now(timezone.utc).isoformat() if using_sqlite() else datetime.now(timezone.utc)
    query = ("UPDATE alerts SET status = " + placeholder + ", assigned_analyst_id = " + placeholder + ", updated_at = " + placeholder + " WHERE id = " + placeholder + " AND user_id = " + placeholder + " RETURNING id, user_id, transaction_id, severity, title, reason, status, assigned_analyst_id, created_at, updated_at")
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, (status, user_id, now, alert_id, user_id)).fetchone()
        return serialize_alert(row) if row else None


def _claim_action_idempotency(connection: Any, *, user_id: int, idempotency_key: str, action_type: str, resource_id: int, request_hash: str) -> dict[str, Any] | None:
    """Claim an action key inside the mutation transaction, or return its completed response."""
    placeholder = "?" if using_sqlite() else "%s"
    insert = (
        "INSERT INTO action_idempotency (user_id, idempotency_key, action_type, resource_id, request_hash) VALUES ("
        + ", ".join([placeholder] * 5) + ") ON CONFLICT(user_id, idempotency_key) DO NOTHING RETURNING id"
    )
    if connection.execute(insert, (user_id, idempotency_key, action_type, resource_id, request_hash)).fetchone():
        return None
    row = connection.execute(
        "SELECT action_type, resource_id, request_hash, response_json FROM action_idempotency WHERE user_id = " + placeholder + " AND idempotency_key = " + placeholder,
        (user_id, idempotency_key),
    ).fetchone()
    existing = dict(row)
    if (existing["action_type"], existing["resource_id"], existing["request_hash"]) != (action_type, resource_id, request_hash):
        raise ValueError("Idempotency key was already used with a different action.")
    if not existing["response_json"]:
        raise DatabaseUnavailableError("An idempotent action is still in progress")
    return json.loads(existing["response_json"])


def set_review_decision_idempotent(*, transaction_id: int, user_id: int, decision: str, idempotency_key: str, request_hash: str, source_hash: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    if not persistence_enabled(): return None
    placeholder = "?" if using_sqlite() else "%s"
    reviewed_at: Any = datetime.now(timezone.utc).isoformat() if using_sqlite() else datetime.now(timezone.utc)
    with get_connection() as connection:
        ensure_schema(connection)
        replay = _claim_action_idempotency(connection, user_id=user_id, idempotency_key=idempotency_key, action_type="manual_review", resource_id=transaction_id, request_hash=request_hash)
        if replay is not None: return replay
        query = ("UPDATE transactions SET review_decision = " + placeholder + ", reviewed_at = " + placeholder + " WHERE id = " + placeholder + " AND user_id = " + placeholder + " RETURNING id, user_id, session_id, amount, amount_minor, idempotency_key, idempotency_request_hash, currency, merchant, sender, receiver, location, device, velocity, transaction_timestamp, risk_score, risk_level, decision, provider, explanation, ai_explanation, analysis_source, processing_time_ms, review_decision, reviewed_at, created_at")
        row = connection.execute(query, (decision, reviewed_at, transaction_id, user_id)).fetchone()
        if not row: raise ActionResourceNotFoundError("Transaction not found")
        result = serialize_transaction(row)
        connection.execute("INSERT INTO audit_events (event_type, user_id, success, source_hash, metadata_json) VALUES (" + ", ".join([placeholder] * 5) + ")", ("manual_review", user_id, True, source_hash, json.dumps(metadata, sort_keys=True, separators=(",", ":"))))
        connection.execute("UPDATE action_idempotency SET response_json = " + placeholder + " WHERE user_id = " + placeholder + " AND idempotency_key = " + placeholder, (json.dumps(result, sort_keys=True, default=str), user_id, idempotency_key))
        return result


def update_alert_status_idempotent(*, alert_id: int, user_id: int, status: str, idempotency_key: str, request_hash: str, source_hash: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    if not persistence_enabled(): return None
    placeholder = "?" if using_sqlite() else "%s"
    updated_at: Any = datetime.now(timezone.utc).isoformat() if using_sqlite() else datetime.now(timezone.utc)
    with get_connection() as connection:
        ensure_schema(connection)
        replay = _claim_action_idempotency(connection, user_id=user_id, idempotency_key=idempotency_key, action_type="alert_status_change", resource_id=alert_id, request_hash=request_hash)
        if replay is not None: return replay
        row = connection.execute("UPDATE alerts SET status = " + placeholder + ", assigned_analyst_id = " + placeholder + ", updated_at = " + placeholder + " WHERE id = " + placeholder + " AND user_id = " + placeholder + " RETURNING id, user_id, transaction_id, severity, title, reason, status, assigned_analyst_id, created_at, updated_at", (status, user_id, updated_at, alert_id, user_id)).fetchone()
        if not row: raise ActionResourceNotFoundError("Alert not found")
        result = serialize_alert(row)
        connection.execute("INSERT INTO audit_events (event_type, user_id, success, source_hash, metadata_json) VALUES (" + ", ".join([placeholder] * 5) + ")", ("alert_status_change", user_id, True, source_hash, json.dumps(metadata, sort_keys=True, separators=(",", ":"))))
        connection.execute("UPDATE action_idempotency SET response_json = " + placeholder + " WHERE user_id = " + placeholder + " AND idempotency_key = " + placeholder, (json.dumps(result, sort_keys=True, default=str), user_id, idempotency_key))
        return result


def get_audit_events_for_user(user_id: int, limit: int = 100) -> list[dict[str, Any]]:
    """Return only the authenticated user's own security events."""
    if not persistence_enabled():
        return []
    placeholder = "?" if using_sqlite() else "%s"
    query = (
        "SELECT id, event_type, user_id, success, source_hash, metadata_json, created_at "
        f"FROM audit_events WHERE user_id = {placeholder} ORDER BY created_at DESC, id DESC LIMIT {placeholder}"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        events = [serialize_audit_event(row) for row in connection.execute(query, (user_id, limit)).fetchall()]
    for event in events:
        event.pop("source_hash", None)
    return events
