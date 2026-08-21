"""Static contract tests for the manually applied PostgreSQL migrations."""

import re
import unittest
from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


class MigrationContractTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (MIGRATIONS / name).read_text(encoding="utf-8")

    def test_preflight_is_read_only_and_checks_supported_baseline(self):
        sql = self.read("000_preflight_sentinelpay_schema.sql")
        self.assertIn("Unsupported SentinelPay schema", sql)
        self.assertIn("transactions schema", sql)
        self.assertIn("amount::numeric <> trunc(amount::numeric, 2)", sql)
        self.assertNotIn("ALTER TABLE", sql)
        self.assertNotIn("UPDATE ", sql)
        self.assertNotIn("CREATE ", sql)

    def test_baseline_bootstrap_is_forward_only_and_requires_an_empty_public_schema(self):
        sql = self.read("000_baseline_sentinelpay_schema.sql")
        upper_sql = sql.upper()
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("SET LOCAL LOCK_TIMEOUT = '5S'", upper_sql)
        self.assertIn("PG_NAMESPACE", upper_sql)
        self.assertIn("NSPNAME = 'PUBLIC'", upper_sql)
        self.assertIn("REQUIRES AN EMPTY PUBLIC SCHEMA", upper_sql)
        executable = "\n".join(
            line for line in upper_sql.splitlines() if not line.lstrip().startswith("--")
        )
        self.assertIsNone(
            re.search(r"(?m)^\s*(DROP|TRUNCATE|DELETE|UPDATE|ALTER\s+TABLE)\b", executable)
        )

    def test_baseline_creates_every_preflight_required_schema_object(self):
        sql = self.read("000_baseline_sentinelpay_schema.sql")
        for table in (
            "users", "transactions", "sessions", "audit_events", "alerts", "rate_limit_buckets",
        ):
            self.assertIn(f"CREATE TABLE {table}", sql)
        for index in (
            "idx_transactions_created_at", "idx_transactions_user_created_at",
            "idx_sessions_token_hash", "idx_audit_events_user_created_at",
            "idx_audit_events_type_created_at", "idx_audit_events_created_at",
            "idx_alerts_user_status_created_at", "idx_rate_limit_buckets_window_started_at",
        ):
            self.assertIn(index, sql)
        self.assertIn("user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE", sql)
        self.assertIn("transaction_id BIGINT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE", sql)
        self.assertIn("PRIMARY KEY (scope, subject_key)", sql)
        self.assertIn("metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb", sql)
        self.assertIn("severity IN ('MEDIUM', 'HIGH', 'CRITICAL')", sql)
        self.assertIn("status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')", sql)

    def test_baseline_defers_rbac_idempotency_and_minor_units_to_existing_upgrades(self):
        baseline = self.read("000_baseline_sentinelpay_schema.sql")
        roles = self.read("001_roles_and_idempotency.sql")
        money = self.read("002_exact_money_and_transaction_atomicity.sql")
        index = self.read("003_transactions_idempotency_index.sql")
        self.assertIn("amount NUMERIC(18,2) NOT NULL", baseline)
        self.assertIn("review_decision TEXT", baseline)
        self.assertIn("reviewed_at TIMESTAMPTZ", baseline)
        self.assertIn("processing_time_ms DOUBLE PRECISION", baseline)
        for later_feature in ("role", "idempotency_key", "idempotency_request_hash", "action_idempotency", "amount_minor"):
            self.assertNotIn(later_feature, baseline)
        self.assertIn("ADD COLUMN IF NOT EXISTS role", roles)
        self.assertIn("ADD COLUMN IF NOT EXISTS idempotency_key", roles)
        self.assertIn("CREATE TABLE IF NOT EXISTS action_idempotency", roles)
        self.assertIn("ADD COLUMN IF NOT EXISTS amount_minor", money)
        self.assertIn("idx_transactions_user_idempotency", index)

    def test_roles_migration_has_repeat_safe_database_constraint(self):
        sql = self.read("001_roles_and_idempotency.sql")
        self.assertIn("users_role_allowed", sql)
        self.assertIn("role IN ('viewer', 'analyst', 'admin')", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS role", sql)
        self.assertIn("VALIDATE CONSTRAINT users_role_allowed", sql)
        self.assertNotIn("DROP ", sql)
        self.assertNotIn("TRUNCATE ", sql)

    def test_money_migration_validates_before_scale_change(self):
        sql = self.read("002_exact_money_and_transaction_atomicity.sql")
        self.assertIn("amount::numeric <> trunc(amount::numeric, 2)", sql)
        self.assertIn("NUMERIC(14,2)", sql)
        self.assertIn("amount_minor", sql)
        self.assertIn("SET LOCAL lock_timeout = '5s'", sql)
        self.assertNotIn("DROP ", sql)
        self.assertNotIn("TRUNCATE ", sql)

    def test_idempotency_index_is_unique_partial_and_concurrent(self):
        sql = self.read("003_transactions_idempotency_index.sql")
        executable = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
        self.assertIn("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS", sql)
        self.assertIn("ON public.transactions (user_id, idempotency_key)", sql)
        self.assertIn("WHERE idempotency_key IS NOT NULL", sql)
        self.assertNotIn("BEGIN", executable)
        self.assertNotIn("COMMIT", executable)


if __name__ == "__main__":
    unittest.main()
