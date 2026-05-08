"""compound_indexes

Revision ID: 1076c0e1bb3f
Revises: be56cd133310
Create Date: 2026-05-08 05:45:00.000000

Replace the single-column idx_quotes_customer index with compound indexes
that match the real query patterns on an FX engine:

  - Customer's pending/executed quotes dashboard
  - Customer transaction history (account statement)
  - Country-based compliance reporting
"""

from typing import Sequence, Union

from alembic import op

revision: str = "1076c0e1bb3f"
down_revision: Union[str, Sequence[str], None] = "be56cd133310"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the naive single-column index added in the initial schema.
    op.execute("DROP INDEX IF EXISTS idx_quotes_customer")

    # "Show me this customer's quotes filtered by status, newest first."
    # Covers: GET /customers/{id}/quotes?status=pending
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_quotes_customer_status_created
            ON quotes(customer_id, status, created_at DESC)
    """)

    # "Show me this customer's transaction history, newest first."
    # Covers: GET /customers/{id}/transactions
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_transactions_customer_date
            ON transactions(customer_id, executed_at DESC)
    """)



def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_transactions_customer_date")
    op.execute("DROP INDEX IF EXISTS idx_quotes_customer_status_created")
    op.execute("CREATE INDEX IF NOT EXISTS idx_quotes_customer ON quotes(customer_id)")
