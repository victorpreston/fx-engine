# Skill: Add a Database Migration

You are working on the FX Engine. Alembic manages all schema changes.

## Never edit existing migration files
Each migration file in `db/versions/` is immutable once committed. Add a new revision for every schema change.

## Steps

### 1. Generate the revision file
```bash
cd fx_engine
DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db \
  alembic revision -m "descriptive_snake_case_name"
```

### 2. Edit the generated file in `db/versions/`
```python
def upgrade() -> None:
    # Use op.execute() for raw SQL — clearer and auditable
    op.execute("""
        ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'standard'
            CONSTRAINT customers_tier_check CHECK (tier IN ('standard', 'premium'))
    """)
    # Always create indexes AFTER the columns they reference
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_customers_tier
            ON customers(tier)
            WHERE tier = 'premium'
    """)

def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_customers_tier")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS tier")
```

### 3. Run locally before committing
```bash
DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db \
  alembic upgrade head
```

Verify with:
```bash
DATABASE_URL=... alembic history
DATABASE_URL=... alembic current
```

### 4. Test
```bash
pytest tests/ -v
```

## Rules
- `IF NOT EXISTS` / `IF EXISTS` in all DDL — migrations are idempotent by convention.
- **Column ordering:** always add columns before creating indexes on them. A partial index on a column that doesn't exist yet will fail at runtime.
- Write `downgrade()` — rollback capability is required in production.
- Migrations run at deploy time (`alembic upgrade head` in Dockerfile), not inside the FastAPI lifespan.

## Composite index guidelines
- Prefix the index with the column that will be in the `WHERE` clause (highest selectivity first).
- For time-sorted queries: `(customer_id, status, created_at DESC)` — not `(created_at, customer_id, status)`.
- For partial indexes, document the predicate condition in a comment.
- Drop superseded single-column indexes in the same migration.
