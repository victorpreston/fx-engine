# Database Migrations (Alembic)

All schema changes go through Alembic. Never edit existing migration files — add a new revision.

## Apply all pending migrations
```bash
cd fx_engine && DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db alembic upgrade head
```

## Create a new migration
```bash
cd fx_engine && alembic revision -m "descriptive_name_here"
# Edit db/versions/<timestamp>_<hash>_descriptive_name_here.py
# Fill in upgrade() with raw SQL — op.execute("ALTER TABLE ...")
# Fill in downgrade() to reverse it
```

## Show migration history
```bash
cd fx_engine && DATABASE_URL=... alembic history
```

## Check current applied version
```bash
cd fx_engine && DATABASE_URL=... alembic current
```

## Roll back one migration
```bash
cd fx_engine && DATABASE_URL=... alembic downgrade -1
```

## Generate SQL script without applying (offline mode)
```bash
cd fx_engine && DATABASE_URL=... alembic upgrade head --sql
```

## Rules
- Always run `alembic upgrade head` locally before committing a new migration
- Never reference a column in an index before it's added in the same (or earlier) migration
- Write both `upgrade()` and `downgrade()` — rollback capability matters in production
- Migrations run at deploy time via Dockerfile, not in the FastAPI lifespan
