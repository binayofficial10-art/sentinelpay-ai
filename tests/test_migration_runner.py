"""Local contract tests for the PostgreSQL migration-ledger runner."""

from __future__ import annotations

import tempfile
import unittest
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

from backend import database, migrations


class _Result:
    def __init__(self, one=None, many=None):
        self._one = one
        self._many = many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _FakePostgresConnection:
    """A small transactional adapter: commits ledger rows, rollback discards them."""

    def __init__(self, *, relations=(), columns=(), constraints=()):
        self.ledger_exists = False
        self.rows: dict[int, tuple[str, str]] = {}
        self.pending_rows: dict[int, tuple[str, str]] = {}
        self.relations = set(relations)
        self.columns = set(columns)
        self.constraints = set(constraints)
        self.executed_scripts: list[str] = []
        self.autocommit = False
        self.script_autocommit: list[bool] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on_script: str | None = None

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        if "CREATE SCHEMA IF NOT EXISTS sentinelpay_meta" in normalized:
            return _Result()
        if "CREATE TABLE IF NOT EXISTS sentinelpay_meta.schema_migrations" in normalized:
            self.ledger_exists = True
            return _Result()
        if normalized.startswith("SELECT to_regclass"):
            name = params[0] if params else "public.transactions"
            exists = name == "sentinelpay_meta.schema_migrations" and self.ledger_exists
            exists = exists or name in self.relations
            return _Result((exists,))
        if normalized.startswith("SELECT version, migration_name, checksum"):
            return _Result(many=[(version, *row) for version, row in sorted(self.rows.items())])
        if normalized.startswith("INSERT INTO sentinelpay_meta.schema_migrations"):
            version, name, checksum = params
            self.pending_rows[version] = (name, checksum)
            return _Result()
        if "information_schema.columns" in normalized and "numeric_precision" in normalized:
            return _Result((14, 2))
        if "SELECT EXISTS (SELECT 1 FROM information_schema.columns" in normalized:
            return _Result(((params[0], params[1]) in self.columns,))
        if "SELECT convalidated FROM pg_constraint" in normalized:
            return _Result((params[0] in self.constraints or params[1] in self.constraints,))
        if normalized.startswith("SELECT pg_advisory_"):
            return _Result((True,))
        if self.fail_on_script and self.fail_on_script in str(sql):
            raise RuntimeError("simulated migration failure")
        self.executed_scripts.append(str(sql))
        self.script_autocommit.append(self.autocommit)
        return _Result()

    def commit(self):
        self.rows.update(self.pending_rows)
        self.pending_rows.clear()
        self.commits += 1

    def rollback(self):
        self.pending_rows.clear()
        self.rollbacks += 1


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self):
        database.close_database_pool()

    def tearDown(self):
        database.close_database_pool()
        self.assertIsNone(database._postgres_pool)

    def migration(self, directory: Path, version: int, name: str, body: str) -> migrations.Migration:
        path = directory / f"{version:03d}_{name}.sql"
        path.write_text(body, encoding="utf-8")
        return migrations.discover_migrations(directory)[-1]

    @contextmanager
    def runner(self, connection, migration_files):
        with (
            patch("backend.migrations.using_sqlite", return_value=False),
            patch("backend.migrations.get_connection", return_value=nullcontext(connection)),
            patch("backend.migrations.discover_migrations", return_value=migration_files),
        ):
            yield

    def test_discovery_is_deterministic_and_ignores_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "010_later.sql").write_text("SELECT 10;", encoding="utf-8")
            (directory / "002_earlier.sql").write_text("SELECT 2;", encoding="utf-8")
            (directory / "000_preflight_ignore.sql").write_text("SELECT 0;", encoding="utf-8")
            (directory / "README.md").write_text("ignore", encoding="utf-8")
            discovered = migrations.discover_migrations(directory)
        self.assertEqual([migration.version for migration in discovered], [2, 10])
        self.assertEqual([migration.name for migration in discovered], ["002_earlier.sql", "010_later.sql"])

    def test_duplicate_versions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            (directory / "001_second.sql").write_text("SELECT 2;", encoding="utf-8")
            with self.assertRaisesRegex(migrations.MigrationError, "unique"):
                migrations.discover_migrations(directory)

    def test_clean_database_applies_and_records_migrations_in_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "000_first.sql").write_text("BEGIN;\nCREATE TABLE first_table ();\nCOMMIT;\n", encoding="utf-8")
            (directory / "001_second.sql").write_text("BEGIN;\nCREATE TABLE second_table ();\nCOMMIT;\n", encoding="utf-8")
            (directory / "002_concurrent_index.sql").write_text("CREATE UNIQUE INDEX CONCURRENTLY idx_test ON test (id);\n", encoding="utf-8")
            migration_files = migrations.discover_migrations(directory)
            connection = _FakePostgresConnection()
            with self.runner(connection, migration_files):
                self.assertEqual(migrations.apply_migrations(), [0, 1, 2])
        self.assertEqual(sorted(connection.rows), [0, 1, 2])
        self.assertEqual(connection.script_autocommit, [False, False, True])

    def test_matching_rerun_skips_applied_migrations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "000_first.sql").write_text("CREATE TABLE first_table ();", encoding="utf-8")
            migration_files = migrations.discover_migrations(directory)
            connection = _FakePostgresConnection()
            with self.runner(connection, migration_files):
                migrations.apply_migrations()
                self.assertEqual(migrations.apply_migrations(), [])
        self.assertEqual(len(connection.executed_scripts), 1)

    def test_checksum_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "000_first.sql").write_text("CREATE TABLE first_table ();", encoding="utf-8")
            migration_files = migrations.discover_migrations(directory)
            connection = _FakePostgresConnection()
            connection.ledger_exists = True
            connection.rows[0] = (migration_files[0].name, "0" * 64)
            with self.runner(connection, migration_files), self.assertRaisesRegex(migrations.MigrationError, "Checksum mismatch"):
                migrations.apply_migrations()
        self.assertEqual(connection.executed_scripts, [])

    def test_failed_migration_rolls_back_and_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "000_first.sql").write_text("CREATE TABLE first_table ();", encoding="utf-8")
            (directory / "001_broken.sql").write_text("CREATE TABLE broken_table ();", encoding="utf-8")
            migration_files = migrations.discover_migrations(directory)
            connection = _FakePostgresConnection()
            connection.fail_on_script = "broken_table"
            with self.runner(connection, migration_files), self.assertRaisesRegex(RuntimeError, "simulated"):
                migrations.apply_migrations()
        self.assertEqual(sorted(connection.rows), [0])
        self.assertGreaterEqual(connection.rollbacks, 1)

    def test_schema_version_reports_current_expected_and_status(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "000_first.sql").write_text("SELECT 1;", encoding="utf-8")
            (directory / "001_second.sql").write_text("SELECT 2;", encoding="utf-8")
            migration_files = migrations.discover_migrations(directory)
            connection = _FakePostgresConnection()
            connection.ledger_exists = True
            connection.rows[0] = (migration_files[0].name, migration_files[0].checksum)
            status = migrations.schema_version(connection, migration_files)
            self.assertEqual(status, {"current_version": 0, "expected_latest_version": 1, "schema_current": False})
            connection.rows[1] = (migration_files[1].name, migration_files[1].checksum)
            self.assertTrue(migrations.schema_version(connection, migration_files)["schema_current"])

    def test_adoption_backfills_only_a_verified_pre_ledger_schema(self):
        migration_files = migrations.discover_migrations()
        relations = {f"public.{name}" for name in migrations._REQUIRED_RELATIONS}
        relations.add("public.idx_transactions_user_idempotency")
        columns = {("transactions", name) for name in ("idempotency_key", "idempotency_request_hash", "amount_minor")}
        columns.add(("users", "role"))
        constraints = set(migrations._REQUIRED_TRANSACTION_CONSTRAINTS) | {"users_role_allowed"}
        connection = _FakePostgresConnection(relations=relations, columns=columns, constraints=constraints)
        with self.runner(connection, migration_files):
            self.assertEqual(migrations.adopt_existing_schema(), [0, 1, 2, 3, 4, 5])
        self.assertEqual(sorted(connection.rows), [0, 1, 2, 3, 4, 5])
        migration_004 = next(migration for migration in migration_files if migration.version == 4)
        self.assertEqual(connection.rows[4], (migration_004.name, migration_004.checksum))

    def test_incompatible_pre_ledger_schema_fails_without_backfill(self):
        migration_files = migrations.discover_migrations()
        connection = _FakePostgresConnection()
        with self.runner(connection, migration_files), self.assertRaisesRegex(migrations.MigrationError, "missing"):
            migrations.adopt_existing_schema()
        self.assertEqual(connection.rows, {})
        self.assertFalse(connection.ledger_exists)

    def test_apply_refuses_pre_ledger_public_schema_without_creating_metadata(self):
        migration_files = migrations.discover_migrations()
        connection = _FakePostgresConnection(relations={"public.transactions"})
        with self.runner(connection, migration_files), self.assertRaisesRegex(migrations.MigrationError, "explicit adoption"):
            migrations.apply_migrations()
        self.assertFalse(connection.ledger_exists)
        self.assertEqual(connection.rows, {})

    def test_cli_status_always_closes_the_process_local_pool(self):
        connection = _FakePostgresConnection()
        with (
            patch.object(sys, "argv", ["backend.migrations", "status"]),
            patch("backend.migrations.get_connection", return_value=nullcontext(connection)),
            patch(
                "backend.migrations.schema_version",
                return_value={"current_version": None, "expected_latest_version": 5, "schema_current": False},
            ),
            patch("backend.migrations.close_database_pool") as close_pool,
        ):
            self.assertEqual(migrations.main(), 0)
        close_pool.assert_called_once()


if __name__ == "__main__":
    unittest.main()
