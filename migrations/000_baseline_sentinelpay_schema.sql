-- Forward-only PostgreSQL bootstrap for an empty SentinelPay staging schema.
-- Run this file explicitly before 000_preflight_sentinelpay_schema.sql.
-- It refuses to coexist with existing public-schema objects and never drops,
-- truncates, resets, or modifies existing data.
BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = public;

-- This bootstrap is deliberately limited to an empty public schema.  A
-- populated or partially provisioned schema requires an explicit migration or
-- remediation plan rather than CREATE IF NOT EXISTS masking a mismatch.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
    ) THEN
        RAISE EXCEPTION
            'SentinelPay baseline bootstrap requires an empty public schema; existing public-schema objects were found. Do not reset or delete them; use an approved forward migration or remediation plan.';
    END IF;
END $$;

CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL DEFAULT 'anonymous',
    amount NUMERIC(18,2) NOT NULL,
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
);

CREATE TABLE audit_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    success BOOLEAN NOT NULL,
    source_hash TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE rate_limit_buckets (
    scope TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    window_started_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (scope, subject_key)
);

CREATE TABLE alerts (
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
);

CREATE INDEX idx_transactions_created_at
ON transactions (created_at DESC, id DESC);

CREATE INDEX idx_transactions_user_created_at
ON transactions (user_id, created_at DESC, id DESC);

CREATE INDEX idx_sessions_token_hash
ON sessions (token_hash);

CREATE INDEX idx_audit_events_user_created_at
ON audit_events (user_id, created_at DESC, id DESC);

CREATE INDEX idx_audit_events_type_created_at
ON audit_events (event_type, created_at DESC, id DESC);

CREATE INDEX idx_audit_events_created_at
ON audit_events (created_at DESC, id DESC);

CREATE INDEX idx_alerts_user_status_created_at
ON alerts (user_id, status, created_at DESC, id DESC);

CREATE INDEX idx_rate_limit_buckets_window_started_at
ON rate_limit_buckets (window_started_at);

COMMIT;
