"""customer_enrichment_and_quote_audit_fields

Revision ID: aadaaac35570
Revises: 1076c0e1bb3f
Create Date: 2026-05-08 05:45:00.000000

Customer enrichment
-------------------
  phone      — primary contact channel for notifications and M-Pesa integration
  country    — ISO 3166-1 alpha-2 country code; required for regulatory reporting
               and future per-country spread tiers
  kyc_status — Know Your Customer verification state (pending / verified / rejected).
               Legally required before executing FX in production.
               Enforced as a future step once auth is introduced; field exists
               so the constraint is database-visible from day one.

Quote audit fields
------------------
  reference  — optional client-provided reference (e.g. "INV-2026-001") for
               reconciliation; stored verbatim, not interpreted by the engine
  mid_rate   — market mid-rate at quote generation time.  Combined with the
               stored effective rate this gives the exact spread applied, which
               is essential for audit trails and dispute resolution.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "aadaaac35570"
down_revision: Union[str, Sequence[str], None] = "1076c0e1bb3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── customers ────────────────────────────────────────────────────────────
    op.execute("""
        ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS phone      TEXT,
            ADD COLUMN IF NOT EXISTS country    CHAR(2),
            ADD COLUMN IF NOT EXISTS kyc_status TEXT NOT NULL DEFAULT 'pending'
                CONSTRAINT customers_kyc_status_check
                    CHECK (kyc_status IN ('pending', 'verified', 'rejected'))
    """)

    # ── quotes ───────────────────────────────────────────────────────────────
    op.execute("""
        ALTER TABLE quotes
            ADD COLUMN IF NOT EXISTS reference TEXT,
            ADD COLUMN IF NOT EXISTS mid_rate  NUMERIC(20, 8)
    """)

    # Index added here (not in 002) because the column must exist first.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_customers_country
            ON customers(country)
            WHERE country IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_customers_country")
    op.execute("ALTER TABLE quotes DROP COLUMN IF EXISTS mid_rate")
    op.execute("ALTER TABLE quotes DROP COLUMN IF EXISTS reference")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS kyc_status")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS country")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS phone")
