from __future__ import annotations

from typing import AsyncGenerator

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=5,
            max_size=20,
            command_timeout=30,
            init=set_type_codecs,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def set_type_codecs(conn: asyncpg.Connection) -> None:
    """Make asyncpg return numeric columns as str so we can wrap in Decimal."""
    await conn.set_type_codec(
        "numeric",
        encoder=str,
        decoder=str,
        schema="pg_catalog",
        format="text",
    )


async def get_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """FastAPI dependency — yields a raw connection from the pool."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
