"""Persistent transaction storage for local SQLite and production PostgreSQL."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class DatabaseUnavailableError(RuntimeError):
    """Raised when persistent storage cannot be reached or configured."""


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOCAL_DATABASE_PATH = Path(__file__).resolve().parent / "sentinelpay.db"

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL NOT NULL,
    sender TEXT NOT NULL,
    receiver TEXT NOT NULL,
    location TEXT NOT NULL,
    device TEXT NOT NULL,
    velocity INTEGER NOT NULL,
    risk_score INTEGER NOT NULL,
    risk_level TEXT NOT NULL,
    decision TEXT NOT NULL,
    ai_explanation TEXT NOT NULL,
    analysis_source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    amount DOUBLE PRECISION NOT NULL,
    sender TEXT NOT NULL,
    receiver TEXT NOT NULL,
    location TEXT NOT NULL,
    device TEXT NOT NULL,
    velocity INTEGER NOT NULL,
    risk_score INTEGER NOT NULL,
    risk_level TEXT NOT NULL,
    decision TEXT NOT NULL,
    ai_explanation TEXT NOT NULL,
    analysis_source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def using_sqlite() -> bool:
    return not DATABASE_URL or DATABASE_URL.startswith("sqlite:///")


def sqlite_path() -> str:
    if DATABASE_URL.startswith("sqlite:///"):
        return DATABASE_URL.removeprefix("sqlite:///")
    return str(LOCAL_DATABASE_PATH)


@contextmanager
def get_connection() -> Iterator[Any]:
    """Open a short-lived database connection suitable for Vercel functions."""
    if using_sqlite():
        connection = sqlite3.connect(sqlite_path())
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            raise DatabaseUnavailableError("Could not access the local SQLite database") from error
        finally:
            connection.close()
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


def ensure_schema(connection: Any) -> None:
    connection.execute(SQLITE_SCHEMA if using_sqlite() else POSTGRES_SCHEMA)


def serialize_transaction(row: Any) -> dict[str, Any]:
    transaction = dict(row)
    created_at = transaction.get("created_at")
    if hasattr(created_at, "isoformat"):
        transaction["created_at"] = created_at.isoformat()
    return transaction


def save_transaction(record: dict[str, Any]) -> dict[str, Any]:
    """Save one assessment and return the stored record, including its database id."""
    columns = (
        "amount", "sender", "receiver", "location", "device", "velocity",
        "risk_score", "risk_level", "decision", "ai_explanation", "analysis_source",
    )
    values = tuple(record[column] for column in columns)
    placeholders = ", ".join("?" if using_sqlite() else "%s" for _ in columns)
    query = (
        f"INSERT INTO transactions ({', '.join(columns)}) VALUES ({placeholders}) "
        "RETURNING id, amount, sender, receiver, location, device, velocity, risk_score, "
        "risk_level, decision, ai_explanation, analysis_source, created_at"
    )

    with get_connection() as connection:
        ensure_schema(connection)
        return serialize_transaction(connection.execute(query, values).fetchone())


def get_recent_transactions(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent assessments newest first."""
    placeholder = "?" if using_sqlite() else "%s"
    query = (
        "SELECT id, amount, sender, receiver, location, device, velocity, risk_score, "
        "risk_level, decision, ai_explanation, analysis_source, created_at "
        f"FROM transactions ORDER BY created_at DESC, id DESC LIMIT {placeholder}"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        return [serialize_transaction(row) for row in connection.execute(query, (limit,)).fetchall()]


def get_transaction(transaction_id: int) -> dict[str, Any] | None:
    """Return one persisted transaction, or None when it does not exist."""
    placeholder = "?" if using_sqlite() else "%s"
    query = (
        "SELECT id, amount, sender, receiver, location, device, velocity, risk_score, "
        "risk_level, decision, ai_explanation, analysis_source, created_at "
        f"FROM transactions WHERE id = {placeholder}"
    )
    with get_connection() as connection:
        ensure_schema(connection)
        row = connection.execute(query, (transaction_id,)).fetchone()
        return serialize_transaction(row) if row else None
