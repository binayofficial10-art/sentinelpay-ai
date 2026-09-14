import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import psycopg

from backend import database
from backend.config import DatabaseSettings


RECORD = {
    "user_id": 1,
    "amount": "25000.00",
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
    def setUp(self):
        database.close_database_pool()

    def tearDown(self):
        database.close_database_pool()

    def fake_pool(self, connection: object | None = None, error: Exception | None = None):
        pool = MagicMock()
        manager = MagicMock()
        if error:
            manager.__enter__.side_effect = error
        else:
            manager.__enter__.return_value = connection or MagicMock()
        pool.connection.return_value = manager
        return pool

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

    def test_direct_sqlite_writes_enforce_transaction_integrity_invariants(self):
        columns = (
            "user_id, amount, amount_minor, currency, merchant, sender, receiver, location, device, velocity, "
            "risk_score, risk_level, decision, provider, explanation, ai_explanation, analysis_source, review_decision"
        )
        valid_values = (
            1, "10.00", 1000, "INR", "merchant", "sender", "receiver", "Delhi", "trusted", 0,
            0, "LOW", "ALLOW", "rule_based_fallback", "fallback", "fallback", "rule_based", None,
        )
        cases = {
            "currency": {"currency": "inr"},
            "velocity": {"velocity": -1},
            "risk_score_below": {"risk_score": -1},
            "risk_score_above": {"risk_score": 101},
            "risk_level": {"risk_level": "CRITICAL"},
            "decision": {"decision": "DENY"},
            "review_decision": {"review_decision": "PENDING"},
            "analysis_source": {"analysis_source": "unknown"},
            "provider": {"provider": "unknown"},
            "analysis_provider_pair": {"analysis_source": "gemini"},
            "zero_amount": {"amount": "0.00", "amount_minor": 0},
            "negative_amount": {"amount": "-0.01", "amount_minor": -1},
            "amount_minor_zero": {"amount_minor": 0},
            "amount_minor_negative": {"amount_minor": -1},
            "amount_minor_mismatch": {"amount_minor": 999},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "transaction-integrity.db"
            with patch.object(database, "DATABASE_URL", f"sqlite:///{database_path.as_posix()}"):
                with database.get_connection() as connection:
                    database.ensure_schema(connection)
                    connection.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'user@example.com', 'test-hash')")
                    placeholders = ", ".join("?" for _ in valid_values)
                    for name, changes in cases.items():
                        row = dict(zip(columns.split(", "), valid_values))
                        row.update(changes)
                        with self.subTest(name=name), self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(
                                f"INSERT INTO transactions ({columns}) VALUES ({placeholders})",
                                tuple(row[column] for column in columns.split(", ")),
                            )
                    connection.execute(
                        f"INSERT INTO transactions ({columns}) VALUES ({placeholders})",
                        valid_values,
                    )
                    self.assertEqual(connection.execute("SELECT count(*) FROM transactions").fetchone()[0], 1)

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
        postgres_connection = MagicMock()
        pool = self.fake_pool(postgres_connection)

        with (
            patch.object(database, "DATABASE_URL", postgres_url),
            patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "production"}, clear=False),
            patch("backend.database.create_postgres_pool", return_value=pool) as create_pool,
            patch("backend.database.sqlite3.connect") as sqlite_connect,
        ):
            self.assertTrue(database.persistence_enabled())
            self.assertFalse(database.using_sqlite())
            with database.get_connection() as connection:
                self.assertIs(connection, postgres_connection)

        create_pool.assert_called_once()
        pool.connection.assert_called_once()
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
        pool = self.fake_pool()
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("backend.database.create_postgres_pool", return_value=pool),
        ):
            with self.assertRaises(database.ActionResourceNotFoundError):
                with database.get_connection():
                    raise database.ActionResourceNotFoundError("Transaction not found")

    def test_postgres_connection_failure_maps_to_database_unavailable(self):
        operational_error = psycopg.OperationalError("offline")
        pool = self.fake_pool(error=operational_error)

        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("backend.database.create_postgres_pool", return_value=pool),
        ):
            with self.assertRaises(database.DatabaseUnavailableError) as raised:
                with database.get_connection():
                    pass

        self.assertIs(raised.exception.__cause__, operational_error)

    def test_postgres_successful_connection_behavior_is_unchanged(self):
        postgres_connection = MagicMock()
        pool = self.fake_pool(postgres_connection)
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("backend.database.create_postgres_pool", return_value=pool),
        ):
            with database.get_connection() as connection:
                self.assertIs(connection, postgres_connection)

        postgres_connection.execute.assert_has_calls(
            [
                call("SELECT set_config('statement_timeout', %s, true)", ("15000",)),
                call(
                    "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
                    ("30000",),
                ),
            ]
        )

    def test_postgres_timeout_configuration_is_applied_on_every_checkout(self):
        settings = DatabaseSettings(1, 3, 8, 4, 12000, 24000)
        postgres_connection = MagicMock()
        pool = self.fake_pool(postgres_connection)
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("backend.database.DatabaseSettings.from_environment", return_value=settings),
            patch("backend.database.create_postgres_pool", return_value=pool),
        ):
            with database.get_connection():
                pass
            with database.get_connection():
                pass

        self.assertEqual(pool.connection.call_count, 2)
        self.assertEqual(
            postgres_connection.execute.call_args_list,
            [
                call("SELECT set_config('statement_timeout', %s, true)", ("12000",)),
                call(
                    "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
                    ("24000",),
                ),
                call("SELECT set_config('statement_timeout', %s, true)", ("12000",)),
                call(
                    "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
                    ("24000",),
                ),
            ],
        )

    def test_postgres_timeout_configuration_failure_closes_connection(self):
        configuration_error = psycopg.OperationalError("configuration failed")
        postgres_connection = MagicMock()
        postgres_connection.execute.side_effect = configuration_error
        pool = self.fake_pool(postgres_connection)
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("backend.database.create_postgres_pool", return_value=pool),
            self.assertRaises(database.DatabaseUnavailableError) as raised,
        ):
            with database.get_connection():
                pass

        self.assertIs(raised.exception.__cause__, configuration_error)
        postgres_connection.close.assert_called_once()

    def test_postgres_pool_is_reused_then_closed_by_reset(self):
        pool = self.fake_pool(MagicMock())
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("backend.database.create_postgres_pool", return_value=pool) as create_pool,
        ):
            with database.get_connection():
                pass
            with database.get_connection():
                pass
            self.assertEqual(create_pool.call_count, 1)
            self.assertEqual(pool.connection.call_count, 2)
            database.close_database_pool()
        pool.close.assert_called_once()

    def test_postgres_pool_factory_applies_bounded_settings(self):
        settings = DatabaseSettings(
            pool_min_size=2,
            pool_max_size=4,
            pool_timeout_seconds=9,
            connect_timeout_seconds=3,
            statement_timeout_ms=12000,
            idle_transaction_timeout_ms=24000,
        )
        created_pool = MagicMock()

        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("psycopg_pool.ConnectionPool", return_value=created_pool) as connection_pool,
        ):
            self.assertIs(database.create_postgres_pool(settings), created_pool)

        connection_pool.assert_called_once_with(
            conninfo="postgresql://database.example.invalid/sentinelpay",
            min_size=2,
            max_size=4,
            timeout=9,
            kwargs={"connect_timeout": 3},
            open=True,
        )

    def test_pool_reset_forces_a_new_isolated_pool(self):
        first_pool = self.fake_pool(MagicMock())
        second_pool = self.fake_pool(MagicMock())
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch(
                "backend.database.create_postgres_pool",
                side_effect=[first_pool, second_pool],
            ) as create_pool,
        ):
            with database.get_connection():
                pass
            database.close_database_pool()
            with database.get_connection():
                pass
            database.close_database_pool()

        self.assertEqual(create_pool.call_count, 2)
        first_pool.close.assert_called_once()
        second_pool.close.assert_called_once()

    def test_postgres_pool_timeout_is_a_controlled_database_error(self):
        from psycopg_pool import PoolTimeout

        pool = self.fake_pool(error=PoolTimeout("timed out"))
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("backend.database.create_postgres_pool", return_value=pool),
            self.assertRaises(database.DatabaseUnavailableError),
        ):
            with database.get_connection():
                pass

    def test_postgres_unexpected_operation_error_is_not_relabelled(self):
        pool = self.fake_pool()
        with (
            patch.object(database, "DATABASE_URL", "postgresql://database.example.invalid/sentinelpay"),
            patch("backend.database.create_postgres_pool", return_value=pool),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected operation"):
                with database.get_connection():
                    raise RuntimeError("unexpected operation")


if __name__ == "__main__":
    unittest.main()
