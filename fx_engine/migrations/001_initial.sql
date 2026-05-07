-- FX Engine schema — idempotent, safe to run multiple times

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── customers ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    email       TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT  customers_email_unique UNIQUE (email)
);

-- ── balances ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS balances (
    customer_id UUID        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    currency    CHAR(3)     NOT NULL,
    amount      NUMERIC(20, 8) NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (customer_id, currency),
    CONSTRAINT  balance_non_negative CHECK (amount >= 0)
);

-- ── quotes ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS quotes (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID        NOT NULL REFERENCES customers(id),
    from_currency   CHAR(3)     NOT NULL,
    to_currency     CHAR(3)     NOT NULL,
    from_amount     NUMERIC(20, 8) NOT NULL,
    to_amount       NUMERIC(20, 8) NOT NULL,
    rate            NUMERIC(20, 8) NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','executed','expired','failed')),
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_quotes_customer ON quotes(customer_id);
CREATE INDEX IF NOT EXISTS idx_quotes_status_expires ON quotes(status, expires_at);

-- ── transactions ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    quote_id        UUID        NOT NULL UNIQUE REFERENCES quotes(id),
    customer_id     UUID        NOT NULL REFERENCES customers(id),
    idempotency_key TEXT,
    from_currency   CHAR(3)     NOT NULL,
    to_currency     CHAR(3)     NOT NULL,
    from_amount     NUMERIC(20, 8) NOT NULL,
    to_amount       NUMERIC(20, 8) NOT NULL,
    rate            NUMERIC(20, 8) NOT NULL,
    status          TEXT        NOT NULL CHECK (status IN ('success','failed')),
    error           TEXT,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_idempotency
    ON transactions(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_transactions_customer ON transactions(customer_id);

-- ── rate_snapshots ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rate_snapshots (
    id          BIGSERIAL   PRIMARY KEY,
    pair        CHAR(7)     NOT NULL,
    mid_rate    NUMERIC(20, 8) NOT NULL,
    source      TEXT        NOT NULL DEFAULT 'api',
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rate_snapshots_pair_time ON rate_snapshots(pair, fetched_at DESC);
