-- Apply after 000 and 001. Run during a maintenance window: ALTER COLUMN
-- rewrites amount and requires an ACCESS EXCLUSIVE lock. lock_timeout makes
-- contention fail safely rather than waiting indefinitely.
BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = public;

DO $$
DECLARE
    invalid_amounts bigint;
BEGIN
    SELECT count(*) INTO invalid_amounts
    FROM transactions
    WHERE amount IS NULL
       OR amount::numeric <= 0
       OR amount::numeric > 999999999999.99
       OR amount::numeric <> trunc(amount::numeric, 2);
    IF invalid_amounts > 0 THEN
        RAISE EXCEPTION 'Cannot safely convert transactions.amount: % invalid value(s) would lose precision or violate bounds.', invalid_amounts;
    END IF;
END $$;

ALTER TABLE transactions
    ALTER COLUMN amount TYPE NUMERIC(14,2) USING amount::numeric;

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS amount_minor BIGINT;

-- PostgreSQL NUMERIC(18,2) values from the prior migration are exact cents.
UPDATE transactions
SET amount_minor = (amount * 100)::BIGINT
WHERE amount_minor IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM transactions WHERE amount_minor IS NULL) THEN
        RAISE EXCEPTION 'Cannot finalize amount_minor: NULL values remain after backfill.';
    END IF;
END $$;

ALTER TABLE transactions ALTER COLUMN amount_minor SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'transactions_amount_minor_positive'
          AND conrelid = 'public.transactions'::regclass
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_amount_minor_positive CHECK (amount_minor > 0 AND amount_minor <= 99999999999999) NOT VALID;
    END IF;
END $$;

ALTER TABLE transactions
    VALIDATE CONSTRAINT transactions_amount_minor_positive;

COMMIT;
