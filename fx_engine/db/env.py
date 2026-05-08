"""
Alembic migration environment.

Uses a synchronous psycopg2 connection so that Alembic can be run as a
plain CLI command (``alembic upgrade head``) before the async API server
starts — no event loop required.

The database URL is read from the DATABASE_URL environment variable (or
the .env file via python-dotenv), so no credentials live in alembic.ini.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

load_dotenv()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Read the database URL from the environment and ensure it uses psycopg2.
# asyncpg is the async runtime driver; psycopg2 is for Alembic's sync CLI.
_db_url = os.environ.get("DATABASE_URL", "")
if _db_url.startswith("postgresql+asyncpg://"):
    _db_url = _db_url.replace("postgresql+asyncpg://", "postgresql://", 1)

config.set_main_option("sqlalchemy.url", _db_url)

# No ORM — raw SQL only. target_metadata stays None.
target_metadata = None


def run_migrations_offline() -> None:
    """Generate SQL script without a live database connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database connection."""
    connectable = create_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
