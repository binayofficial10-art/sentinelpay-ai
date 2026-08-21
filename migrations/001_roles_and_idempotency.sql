-- Apply after 000_preflight_sentinelpay_schema.sql.
-- Transactional schema change only; the concurrent index is migration 003.
BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = public;

ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'viewer';
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS idempotency_request_hash TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass
          AND conname = 'users_role_allowed'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT users_role_allowed
            CHECK (role IN ('viewer', 'analyst', 'admin')) NOT VALID;
    END IF;
END $$;

ALTER TABLE users VALIDATE CONSTRAINT users_role_allowed;

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
);

COMMIT;
