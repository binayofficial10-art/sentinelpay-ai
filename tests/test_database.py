import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend import database


RECORD = {
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
    def test_sqlite_local_mode_saves_and_retrieves_transactions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "sentinelpay-test.db"
            sqlite_url = f"sqlite:///{database_path.as_posix()}"
            with (
                patch.object(database, "DATABASE_URL", sqlite_url),
                patch.dict(os.environ, {"VERCEL": "", "VERCEL_ENV": ""}, clear=False),
            ):
                saved = database.save_transaction(RECORD)
                recent = database.get_recent_transactions()
                retrieved = database.get_transaction(saved["id"])

        self.assertIsNotNone(saved)
        self.assertEqual(saved["amount"], RECORD["amount"])
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
                migrated = database.get_recent_transactions()

        self.assertEqual(migrated[0]["merchant"], "legacy-merchant")
        self.assertEqual(migrated[0]["currency"], "INR")
        self.assertEqual(migrated[0]["provider"], "gemini")
        self.assertEqual(migrated[0]["explanation"], "legacy assessment")

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


if __name__ == "__main__":
    unittest.main()
