import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend import database


RECORD = {
    "user_id": 1,
    "amount": 25000.0,
    "sender": "user123",
    "receiver": "merchant456",
    "location": "Bhubaneswar",
    "device": "trusted",
    "velocity": 8,
    "risk_score": 35,
    "risk_level": "LOW",
    "decision": "ALLOW",
    "ai_explanation": "Rule-based fraud assessment because Gemini is unavailable.",
    "analysis_source": "rule_based",
}


class DatabaseTests(unittest.TestCase):
    def postgres_modules(self, connect):
        """Provide the small psycopg surface get_connection imports."""
        driver = types.ModuleType("psycopg")
        rows = types.ModuleType("psycopg.rows")

        class DriverError(Exception):
            pass

        class OperationalError(DriverError):
            pass

        driver.connect = connect
        driver.Error = DriverError
        driver.OperationalError = OperationalError
        rows.dict_row = object()
        return {"psycopg": driver, "psycopg.rows": rows}, OperationalError

    def test_sqlite_local_mode_saves_and_retrieves_transactions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "sentinelpay-test.db"
            sqlite_url = f"sqlite:///{database_path.as_posix()}"
            with (
                patch.object(database, "DATABASE_URL", sqlite_url),
                patch.dict(os.environ, {"VERCEL": "", "VERCEL_ENV": ""}, clear=False),
            ):
                with database.get_connection() as connection:
                    database.ensure_schema(connection)
                    connection.execute(
                        "INSERT INTO users (id, email, password_hash) VALUES (1, 'user@example.com', 'test-hash')"
                    )
                saved = database.save_transaction(RECORD)
                recent = database.get_recent_transactions(RECORD["user_id"])
                retrieved = database.get_transaction(saved["id"], RECORD["user_id"])

        self.assertIsNotNone(saved)
        self.assertEqual(saved["amount"], "25000.00")
        self.assertEqual(saved["amount_minor"], 2_500_000)
        self.assertEqual(recent[0]["id"], saved["id"])
        self.assertEqual(retrieved["sender"], RECORD["sender"])
        self.assertEqual(retrieved["analysis_source"], RECORD["analysis_source"])
        self.assertEqual(retrieved["currency"], "INR")
        self.assertEqual(retrieved["merchant"], RECORD["receiver"])
        self.assertEqual(retrieved["provider"], "rule_based_fallback")
        self.assertEqual(retrieved["explanation"], RECORD["ai_explanation"])
        self.assertEqual(retrieved["session_id"], "anonymous")
        self.assertTrue(retrieved["transaction_timestamp"])

    def test_database_unavailable_raises_a_safe_storage_error(self):
        with (
            patch.object(database, "DATABASE_URL", "sqlite:///unavailable.db"),
            patch.dict(os.environ, {"VERCEL": "", "VERCEL_ENV": ""}, clear=False),
            patch("backend.database.sqlite3.connect", side_effect=sqlite3.Error("unavailable")),
        ):
            with self.assertRaises(database.DatabaseUnavailableError):
                database.save_transaction(RECORD)

    def test_sqlite_audit_events_and_shared_rate_limit_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "security-test.db"
            sqlite_url = f"sqlite:///{database_path.as_posix()}"
            with (
                patch.object(database, "DATABASE_URL", sqlite_url),
                patch.dict(os.environ, {"VERCEL": "", "VERCEL_ENV": ""}, clear=False),
            ):
                with database.get_connection() as connection:
                    database.ensure_schema(connection)
                    connection.execute(
                        "INSERT INTO users (id, email, password_hash) VALUES (1, 'user@example.com', 'test-hash')"
                    )
                database.write_audit_event(
                    event_type="login", success=True, user_id=1, source_hash="fingerprint", metadata={"path": "/auth/login"}
                )
                first_count, _ = database.consume_rate_limit(scope="login", subject_key="source:fingerprint", window_seconds=60)
                second_count, _ = database.consume_rate_limit(scope="login", subject_key="source:fingerprint", window_seconds=60)
                events = database.get_audit_events_for_user(1)

        self.assertEqual((first_count, second_count), (1, 2))
        self.assertEqual(events[0]["event_type"], "login")
        self.assertNotIn("source_hash", events[0])

    def test_database_unique_idempotency_constraint_returns_no_second_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "idempotency-test.db"
            sqlite_url = f"sqlite:///{database_path.as_posix()}"
            with patch.object(database, "DATABASE_URL", sqlite_url):
                with database.get_connection() as connection:
                    database.ensure_schema(connection)
                    connection.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'user@example.com', 'test-hash')")
                first = database.save_transaction({**RECORD, "idempotency_key": "txn-unique-key", "idempotency_request_hash": "hash-a"})
                second = database.save_transaction({**RECORD, "idempotency_key": "txn-unique-key", "idempotency_request_hash": "hash-a"})
                stored = database.get_transaction_by_idempotency_key("txn-unique-key", 1)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(stored["id"], first["id"])

    def test_minor_unit_database_constraint_rejects_non_positive_amounts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "money-constraint.db"
            with patch.object(database, "DATABASE_URL", f"sqlite:///{database_path.as_posix()}"):
                with database.get_connection() as connection:
                    database.ensure_schema(connection)
                    connection.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'user@example.com', 'test-hash')")
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "INSERT INTO transactions (user_id, amount, amount_minor, merchant, sender, receiver, location, device, velocity, risk_score, risk_level, decision, provider, explanation, ai_explanation, analysis_source) "
                            "VALUES (1, '0.00', 0, 'merchant', 'sender', 'receiver', 'Delhi', 'trusted', 1, 0, 'LOW', 'ALLOW', 'rule_based_fallback', 'fallback', 'fallback', 'rule_based')"
                        )

    def test_sqlite_migrates_existing_transaction_history_additively(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "legacy-sentinelpay.db"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE transactions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL NOT NULL, sender TEXT NOT NULL, "
                "receiver TEXT NOT NULL, location TEXT NOT NULL, device TEXT NOT NULL, velocity INTEGER NOT NULL, "
                "risk_score INTEGER NOT NULL, risk_level TEXT NOT NULL, decision TEXT NOT NULL, "
                "ai_explanation TEXT NOT NULL, analysis_source TEXT NOT NULL, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO transactions (amount, sender, receiver, location, device, velocity, risk_score, "
                "risk_level, decision, ai_explanation, analysis_source) "
                "VALUES (1, 'legacy-user', 'legacy-merchant', 'Delhi', 'trusted', 1, 0, 'LOW', 'ALLOW', "
                "'legacy assessment', 'gemini')"
            )
            connection.commit()
            connection.close()

            sqlite_url = f"sqlite:///{database_path.as_posix()}"
            with (
                patch.object(database, "DATABASE_URL", sqlite_url),
                patch.dict(os.environ, {"VERCEL": "", "VERCEL_ENV": ""}, clear=False),
            ):
                with database.get_connection() as migrated_connection:
                    database.ensure_schema(migrated_connection)
                    migrated = dict(
                        migrated_connection.execute(
                            "SELECT merchant, currency, provider, explanation FROM transactions"
                        ).fetchone()
                    )

        self.assertEqual(migrated["merchant"], "legacy-merchant")
        self.assertEqual(migrated["currency"], "INR")
        self.assertEqual(migrated["provider"], "gemini")
        self.assertEqual(migrated["explanation"], "legacy assessment")

    def test_vercel_uses_postgresql_database_url_without_opening_sqlite(self):
        postgres_url = "postgresql://database.example.invalid/sentinelpay"
        managed_connection = MagicMock()
        postgres_connection = MagicMock()
        managed_connection.__enter__.return_value = postgres_connection

        with (
            patch.object(database, "DATABASE_URL", postgres_url),
            patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "production"}, clear=False),
            patch("psycopg.connect", return_value=managed_connection) as connect,
            patch("backend.database.sqlite3.connect") as sqlite_connect,
        ):
            self.assertTrue(database.persistence_enabled())
            self.assertFalse(database.using_sqlite())
            with database.get_connection() as connection:
                self.assertIs(connection, postgres_connection)

        connect.assert_called_once()
        self.assertEqual(connect.call_args.args[0], postgres_url)
        sqlite_connect.assert_not_called()

    def test_vercel_without_database_url_disables_persistence(self):
        with (
            patch.object(database, "DATABASE_URL", ""),
            patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "production"}, clear=False),
            patch("backend.database.sqlite3.connect") as sqlite_connect,
        ):
            self.assertFalse(database.persistence_enabled())
            self.assertIsNone(database.save_transaction(RECORD))

        sqlite_connect.assert_not_called()

    def test_postgres_action_resource_not_found_is_preserved(self):
        managed_connection = MagicMock()
        modules, _ = self.postgres_modules(MagicMock(return_value=managed_connection))
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch.dict(sys.modules, modules),
        ):
            with self.assertRaises(database.ActionResourceNotFoundError):
                with database.get_connection():
                    raise database.ActionResourceNotFoundError("Transaction not found")

    def test_postgres_connection_failure_maps_to_database_unavailable(self):
        modules, operational_error = self.postgres_modules(MagicMock())
        modules["psycopg"].connect.side_effect = operational_error("offline")

        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch.dict(sys.modules, modules),
        ):
            with self.assertRaises(database.DatabaseUnavailableError) as raised:
                with database.get_connection():
                    pass

        self.assertIsInstance(raised.exception.__cause__, operational_error)

    def test_postgres_successful_connection_behavior_is_unchanged(self):
        managed_connection = MagicMock()
        postgres_connection = MagicMock()
        managed_connection.__enter__.return_value = postgres_connection
        modules, _ = self.postgres_modules(MagicMock(return_value=managed_connection))
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch.dict(sys.modules, modules),
        ):
            with database.get_connection() as connection:
                self.assertIs(connection, postgres_connection)

    def test_postgres_unexpected_operation_error_is_not_relabelled(self):
        managed_connection = MagicMock()
        modules, _ = self.postgres_modules(MagicMock(return_value=managed_connection))
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch.dict(sys.modules, modules),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected operation"):
                with database.get_connection():
                    raise RuntimeError("unexpected operation")


if __name__ == "__main__":
    unittest.main()
