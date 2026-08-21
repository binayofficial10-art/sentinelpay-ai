"""Persistent transaction storage for local SQLite and production PostgreSQL."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class DatabaseUnavailableError(RuntimeError):
    """Raised when persistent storage cannot be reached or configured."""


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOCAL_DATABASE_PATH = Path(__file__).resolve().parent / "sentinelpay.db"

SQLITE_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
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

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL DEFAULT 'anonymous',
    amount REAL NOT NULL,
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL DEFAULT 'anonymous',
    amount DOUBLE PRECISION NOT NULL,
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

SESSION_TOKEN_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash
ON sessions (token_hash)
"""

POSTGRES_MIGRATIONS = (
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id) ON DELETE CASCADE",
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT 'anonymous'",
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'INR'",
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS merchant TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS transaction_timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'rule_based_fallback'",
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS explanation TEXT NOT NULL DEFAULT ''",
    "UPDATE transactions SET merchant = receiver WHERE merchant = ''",
    "UPDATE transactions SET provider = 'gemini' WHERE analysis_source = 'gemini' AND provider = 'rule_based_fallback'",
    "UPDATE transactions SET explanation = ai_explanation WHERE explanation = ''",
)

SQLITE_MIGRATION_COLUMNS = {
    "user_id": "INTEGER",
    "session_id": "TEXT NOT NULL DEFAULT 'anonymous'",
    "currency": "TEXT NOT NULL DEFAULT 'INR'",
    "merchant": "TEXT NOT NULL DEFAULT ''",
    "transaction_timestamp": "TEXT NOT NULL DEFAULT ''",
    "provider": "TEXT NOT NULL DEFAULT 'rule_based_fallback'",
    "explanation": "TEXT NOT NULL DEFAULT ''",
}


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
    except Exception as error:
        raise DatabaseUnavailableError("Could not connect to DATABASE_URL") from error


def initialize_database(connection: Any) -> None:
    """Apply repeatable schema initialization for local SQLite or PostgreSQL."""
    connection.execute(SQLITE_USERS_SCHEMA if using_sqlite() else POSTGRES_USERS_SCHEMA)
    connection.execute(SQLITE_SESSIONS_SCHEMA if using_sqlite() else POSTGRES_SESSIONS_SCHEMA)
    connection.execute(SQLITE_SCHEMA if using_sqlite() else POSTGRES_SCHEMA)
    if using_sqlite():
        existing_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(transactions)").fetchall()
        }
        for column, definition in SQLITE_MIGRATION_COLUMNS.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE transactions ADD COLUMN {column} {definition}")
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
    else:
        for statement in POSTGRES_MIGRATIONS:
            connection.execute(statement)
    connection.execute(TRANSACTION_INDEX_SCHEMA)
    connection.execute(TRANSACTION_USER_INDEX_SCHEMA)
    connection.execute(SESSION_TOKEN_INDEX_SCHEMA)


def ensure_schema(connection: Any) -> None:
    """Backward-compatible name for repeatable database initialization."""
    initialize_database(connection)


def serialize_transaction(row: Any) -> dict[str, Any]:
    transaction = dict(row)
    for timestamp_column in ("transaction_timestamp", "created_at"):
        timestamp = transaction.get(timestamp_column)
        if hasattr(timestamp, "isoformat"):
            transaction[timestamp_column] = timestamp.isoformat()
    return transaction


def save_transaction(record: dict[str, Any]) -> dict[str, Any] | None:
    """Save one assessment and return the stored record, including its database id."""
    if not persistence_enabled():
        return None

    explanation = record.get("explanation", record["ai_explanation"])
    columns = (
        "user_id", "session_id", "amount", "currency", "merchant", "sender", "receiver", "location",
        "device", "velocity", "transaction_timestamp", "risk_score", "risk_level", "decision",
        "provider", "explanation", "ai_explanation", "analysis_source",
    )
    values_by_column = {
        "user_id": record["user_id"],
        "session_id": record.get("session_id", "anonymous"),
        "amount": record["amount"],
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
    }
    if using_sqlite():
        transaction_timestamp = values_by_column["transaction_timestamp"]
        if hasattr(transaction_timestamp, "isoformat"):
            values_by_column["transaction_timestamp"] = transaction_timestamp.isoformat()
    values = tuple(values_by_column[column] for column in columns)
    placeholders = ", ".join("?" if using_sqlite() else "%s" for _ in columns)
    query = (
        f"INSERT INTO transactions ({', '.join(columns)}) VALUES ({placeholders}) "
        "RETURNING id, user_id, session_id, amount, currency, merchant, sender, receiver, location, device, "
        "velocity, transaction_timestamp, risk_score, risk_level, decision, provider, explanation, "
        "ai_explanation, analysis_source, created_at"
    )

    with get_connection() as connection:
        ensure_schema(connection)
        return serialize_transaction(connection.execute(query, values).fetchone())


def get_recent_transactions(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent assessments newest first."""
    if not persistence_enabled():
        return []

    placeholder = "?" if using_sqlite() else "%s"
    query = (
        "SELECT id, user_id, session_id, amount, currency, merchant, sender, receiver, location, device, velocity, "
        "transaction_timestamp, risk_score, risk_level, decision, provider, explanation, ai_explanation, "
        "analysis_source, created_at "
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
        "SELECT id, user_id, session_id, amount, currency, merchant, sender, receiver, location, device, velocity, "
        "transaction_timestamp, risk_score, risk_level, decision, provider, explanation, ai_explanation, "
        "analysis_source, created_at "
        f"FROM transactions WHERE id = {placeholder} AND user_id = {placeholder}"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, (transaction_id, user_id)).fetchone()
        return serialize_transaction(row) if row else None
