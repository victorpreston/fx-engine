"""initial_schema

Revision ID: be56cd133310
Revises:
Create Date: 2026-05-08 05:27:23.333422

"""

from typing import Sequence, Union

from alembic import op

revision: str = "be56cd133310"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    op.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT        NOT NULL,
            email       TEXT        NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT  customers_email_unique UNIQUE (email)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS balances (
            customer_id UUID        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            currency    CHAR(3)     NOT NULL,
            amount      NUMERIC(20, 8) NOT NULL DEFAULT 0,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (customer_id, currency),
            CONSTRAINT  balance_non_negative CHECK (amount >= 0)
        )
    """)

    op.execute("""
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
        )
    """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_customer ON quotes(customer_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_status_expires "
        "ON quotes(status, expires_at)"
    )

    op.execute("""
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
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_idempotency
            ON transactions(idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_customer "
        "ON transactions(customer_id)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS rate_snapshots (
            id          BIGSERIAL   PRIMARY KEY,
            pair        CHAR(7)     NOT NULL,
            mid_rate    NUMERIC(20, 8) NOT NULL,
            source      TEXT        NOT NULL DEFAULT 'api',
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_snapshots_pair_time "
        "ON rate_snapshots(pair, fetched_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rate_snapshots CASCADE")
    op.execute("DROP TABLE IF EXISTS transactions CASCADE")
    op.execute("DROP TABLE IF EXISTS quotes CASCADE")
    op.execute("DROP TABLE IF EXISTS balances CASCADE")
    op.execute("DROP TABLE IF EXISTS customers CASCADE")
