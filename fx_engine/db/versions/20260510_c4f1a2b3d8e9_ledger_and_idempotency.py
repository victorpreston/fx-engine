"""ledger_entries and idempotency_keys

Revision ID: c4f1a2b3d8e9
Revises: aadaaac35570
Create Date: 2026-05-10 09:00:00.000000

ledger_entries
--------------
  Append-only double-entry ledger.  Every credit_balance call and every
  execute_quote call inserts rows here inside the same transaction as the
  balance UPDATE.  balances is a materialized cache; ledger_entries is the
  source of truth.  Invariant: balance == SUM(credits) - SUM(debits) per
  (customer_id, currency).

  direction       — 'debit' (money leaving customer) or 'credit' (money arriving)
  reference_type  — 'execution' or 'credit_adjustment'
  reference_id    — UUID of the triggering record (transaction or future adj record)

idempotency_keys
----------------
  Stores the request hash and response payload for each (endpoint, key) pair.
  Allows detecting same-key-different-payload conflicts (409) and replaying
  identical requests (return stored response, 200).

  endpoint        — e.g. 'POST /quotes/{id}/execute'
  request_hash    — SHA-256 of method+path+body; binding the key to payload
  response_payload— JSONB snapshot of the response at completion time
  completed_at    — NULL while in-flight; set when the response is stored
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c4f1a2b3d8e9"
down_revision: Union[str, Sequence[str], None] = "aadaaac35570"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ledger_entries (
            id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            customer_id     UUID        NOT NULL REFERENCES customers(id),
            currency        CHAR(3)     NOT NULL,
            amount          NUMERIC(20, 8) NOT NULL,
            direction       TEXT        NOT NULL CHECK (direction IN ('debit', 'credit')),
            reference_type  TEXT        NOT NULL CHECK (reference_type IN ('execution', 'credit_adjustment')),
            reference_id    UUID        NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ledger_entries_amount_positive CHECK (amount > 0)
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_ref_direction
            ON ledger_entries(reference_id, reference_type, direction)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ledger_customer_currency
            ON ledger_entries(customer_id, currency)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            endpoint        TEXT        NOT NULL,
            key             TEXT        NOT NULL,
            request_hash    TEXT        NOT NULL,
            response_payload JSONB,
            status_code     INT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at    TIMESTAMPTZ,
            CONSTRAINT idempotency_keys_endpoint_key_unique UNIQUE (endpoint, key)
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created_at
            ON idempotency_keys(created_at)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS idempotency_keys CASCADE")
    op.execute("DROP TABLE IF EXISTS ledger_entries CASCADE")
