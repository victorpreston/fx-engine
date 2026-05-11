"""ledger_entries immutability trigger

Revision ID: f2e3a4b5c6d7
Revises: c4f1a2b3d8e9
Create Date: 2026-05-10 10:00:00.000000

Adds a BEFORE UPDATE OR DELETE trigger on ledger_entries that raises an
exception on any attempt to mutate a committed row.  Corrections to the
ledger must be made by inserting a reversing entry, not by editing history.

The trigger function is created with CREATE OR REPLACE so re-running the
migration is idempotent.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f2e3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "c4f1a2b3d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_ledger_modification()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'ledger_entries rows are immutable — insert a reversing entry instead';
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries
    """)

    op.execute("""
        CREATE TRIGGER ledger_entries_immutable
            BEFORE UPDATE OR DELETE ON ledger_entries
            FOR EACH ROW EXECUTE FUNCTION reject_ledger_modification()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS reject_ledger_modification()")
