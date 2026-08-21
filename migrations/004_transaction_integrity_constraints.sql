-- Apply after 000, 001, 002, and 003.  This forward-only migration adds
-- database-level transaction invariants without rewriting financial records.
-- Any incompatible existing row makes validation fail and rolls back the whole
-- migration for explicit remediation.
BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = public;

-- Migration 002 established the supported exact-money representation.  Do not
-- silently coerce a divergent schema here: fail clearly instead.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'transactions'
          AND column_name = 'amount'
          AND data_type = 'numeric'
          AND numeric_precision = 14
          AND numeric_scale = 2
    ) THEN
        RAISE EXCEPTION
            'Unsupported transactions.amount definition: expected NUMERIC(14,2) from migration 002. Do not coerce existing financial data; remediate the schema explicitly.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_currency_format') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_currency_format
            CHECK (currency ~ '^[A-Z]{3}$') NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_velocity_nonnegative') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_velocity_nonnegative
            CHECK (velocity >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_risk_score_range') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_risk_score_range
            CHECK (risk_score BETWEEN 0 AND 100) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_risk_level_allowed') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_risk_level_allowed
            CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_decision_allowed') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_decision_allowed
            CHECK (decision IN ('ALLOW', 'REVIEW', 'BLOCK')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_review_decision_allowed') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_review_decision_allowed
            CHECK (review_decision IS NULL OR review_decision IN ('APPROVE', 'BLOCK')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_analysis_source_allowed') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_analysis_source_allowed
            CHECK (analysis_source IN ('gemini', 'rule_based')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_provider_allowed') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_provider_allowed
            CHECK (provider IN ('gemini', 'rule_based_fallback')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_analysis_provider_match') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_analysis_provider_match
            CHECK (
                (analysis_source = 'gemini' AND provider = 'gemini')
                OR (analysis_source = 'rule_based' AND provider = 'rule_based_fallback')
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_amount_positive') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_amount_positive
            CHECK (amount > 0) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.transactions'::regclass AND conname = 'transactions_amount_minor_consistent') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_amount_minor_consistent
            CHECK (amount = amount_minor::numeric / 100) NOT VALID;
    END IF;
END $$;

DO $$
DECLARE
    constraint_name text;
BEGIN
    FOREACH constraint_name IN ARRAY ARRAY[
        'transactions_currency_format',
        'transactions_velocity_nonnegative',
        'transactions_risk_score_range',
        'transactions_risk_level_allowed',
        'transactions_decision_allowed',
        'transactions_review_decision_allowed',
        'transactions_analysis_source_allowed',
        'transactions_provider_allowed',
        'transactions_analysis_provider_match',
        'transactions_amount_positive',
        'transactions_amount_minor_consistent'
    ] LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'public.transactions'::regclass
              AND conname = constraint_name
              AND NOT convalidated
        ) THEN
            EXECUTE format('ALTER TABLE public.transactions VALIDATE CONSTRAINT %I', constraint_name);
        END IF;
    END LOOP;
END $$;

COMMIT;
