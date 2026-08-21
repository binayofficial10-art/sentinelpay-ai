-- Read-only preflight. Run this first with psql -v ON_ERROR_STOP=1 -f.
-- It validates the supported SentinelPay baseline and makes no data changes.
DO $$
DECLARE
    required_transaction_columns text[] := ARRAY[
        'id', 'user_id', 'amount', 'sender', 'receiver', 'location', 'device',
        'velocity', 'risk_score', 'risk_level', 'decision', 'ai_explanation',
        'analysis_source', 'created_at'
    ];
    missing_columns text[];
    amount_type text;
    invalid_amounts bigint;
    duplicate_keys bigint;
    invalid_roles bigint;
BEGIN
    IF to_regclass('public.users') IS NULL OR to_regclass('public.transactions') IS NULL
       OR to_regclass('public.sessions') IS NULL OR to_regclass('public.audit_events') IS NULL
       OR to_regclass('public.alerts') IS NULL OR to_regclass('public.rate_limit_buckets') IS NULL THEN
        RAISE EXCEPTION 'Unsupported SentinelPay schema: expected users, transactions, sessions, audit_events, alerts, and rate_limit_buckets in public. Restore or migrate to the supported baseline before continuing.';
    END IF;

    SELECT array_agg(column_name ORDER BY column_name)
    INTO missing_columns
    FROM unnest(required_transaction_columns) AS required(column_name)
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'transactions'
          AND information_schema.columns.column_name = required.column_name
    );
    IF missing_columns IS NOT NULL THEN
        RAISE EXCEPTION 'Unsupported SentinelPay transactions schema; missing required columns: %', array_to_string(missing_columns, ', ');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.transactions'::regclass
          AND contype = 'f'
          AND confrelid = 'public.users'::regclass
    ) THEN
        RAISE EXCEPTION 'Unsupported SentinelPay transactions schema; a foreign key from transactions to users is required.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'id'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'email'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'password_hash'
    ) THEN
        RAISE EXCEPTION 'Unsupported SentinelPay users schema; users.id, users.email, and users.password_hash are required.';
    END IF;

    SELECT data_type INTO amount_type
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'transactions' AND column_name = 'amount';
    IF amount_type NOT IN ('double precision', 'real', 'numeric') THEN
        RAISE EXCEPTION 'Unsupported transactions.amount type: %. Expected real, double precision, or numeric.', amount_type;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'role'
    ) THEN
        EXECUTE 'SELECT count(*) FROM public.users WHERE role IS NULL OR role NOT IN (''viewer'', ''analyst'', ''admin'')'
        INTO invalid_roles;
        IF invalid_roles > 0 THEN
            RAISE EXCEPTION 'Cannot validate roles: % existing user role(s) are unsupported. Remediate explicitly before migration.', invalid_roles;
        END IF;
    END IF;

    -- A scale-changing conversion must never silently round an old value.
    SELECT count(*) INTO invalid_amounts
    FROM transactions
    WHERE amount IS NULL
       OR amount::numeric <= 0
       OR amount::numeric > 999999999999.99
       OR amount::numeric <> trunc(amount::numeric, 2);
    IF invalid_amounts > 0 THEN
        RAISE EXCEPTION 'Cannot safely migrate % transaction amount(s): values must be positive, <= 999999999999.99, and have at most two decimal places. Remediate explicitly before migration.', invalid_amounts;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'transactions' AND column_name = 'idempotency_key'
    ) THEN
        SELECT count(*) INTO duplicate_keys
        FROM (
            SELECT user_id, idempotency_key
            FROM transactions
            WHERE idempotency_key IS NOT NULL
            GROUP BY user_id, idempotency_key
            HAVING count(*) > 1
        ) duplicates;
        IF duplicate_keys > 0 THEN
            RAISE EXCEPTION 'Cannot create the idempotency index: % duplicate user/key pair(s) exist. Resolve duplicates explicitly before migration.', duplicate_keys;
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_class indexes
        JOIN pg_namespace namespaces ON namespaces.oid = indexes.relnamespace
        WHERE indexes.relkind = 'i' AND namespaces.nspname = 'public'
          AND indexes.relname = 'idx_transactions_user_idempotency'
    ) THEN
        -- The index is intentionally absent before migration 003.  Keep the
        -- regclass lookup inside this procedural branch so PostgreSQL never
        -- resolves a nonexistent relation during pre-003 validation.
        IF NOT EXISTS (
            SELECT 1
            FROM pg_index
            WHERE indexrelid = 'public.idx_transactions_user_idempotency'::regclass
              AND indisunique AND indisvalid
              AND pg_get_expr(indpred, indrelid) = '(idempotency_key IS NOT NULL)'
        ) THEN
            RAISE EXCEPTION 'Existing idx_transactions_user_idempotency is invalid or does not enforce the supported unique partial-index definition.';
        END IF;
    END IF;
END $$;
