-- Apply after 000 through 004.  The runner records immutable checksums for
-- every versioned migration in this metadata schema; no application data is
-- modified.
BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE SCHEMA IF NOT EXISTS sentinelpay_meta;

CREATE TABLE IF NOT EXISTS sentinelpay_meta.schema_migrations (
    version INTEGER PRIMARY KEY,
    migration_name TEXT NOT NULL UNIQUE,
    checksum CHAR(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMIT;
