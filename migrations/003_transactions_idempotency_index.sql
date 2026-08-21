-- Apply after 000, 001, and 002 using psql with autocommit enabled.
-- CREATE INDEX CONCURRENTLY cannot run inside BEGIN/COMMIT or a transaction.
-- If this fails because duplicate keys exist, do not deploy; resolve the
-- reported data issue explicitly, rerun preflight, then rerun this file.
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_transactions_user_idempotency
ON public.transactions (user_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;
