"""
Test fixtures.

Event-loop model
----------------
asyncio_default_fixture_loop_scope = "function"

Each test gets its own fresh asyncio event loop.  Fixtures (all
function-scoped) share that same loop, so every asyncpg connection and
pool is always bound to the exact loop that is running the test.
There is no session-vs-function loop mismatch.

db_schema is the one exception: it is session-scoped and must run
migrations exactly once before any test.  Because it cannot use a
function-scoped loop that doesn't exist yet, it is synchronous and
calls asyncio.run() internally to spin up its own temporary loop.

Requires a running PostgreSQL instance:

    docker compose -f docker-compose.test.yml up -d
    TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db pytest
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db",
    ),
)
os.environ.setdefault("RATE_STALE_SECONDS", "3600")
os.environ.setdefault("ENVIRONMENT", "test")

from app.config import settings  # noqa: E402
from app.database import run_migrations, set_type_codecs  # noqa: E402
from app.main import app  # noqa: E402
from app.rates import rate_provider, _FALLBACK_MID, _compute_all_mids  # noqa: E402
import app.database as _db_module  # noqa: E402


# ── Schema (session-scoped, synchronous) ──────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def db_schema():
    """
    Run migrations once before the first test.

    Synchronous so it creates its own temporary event loop via asyncio.run().
    This avoids the fixture needing to share a loop with any test.
    """
    async def _migrate():
        pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=1,
            init=set_type_codecs,
        )
        try:
            await run_migrations(pool)
        finally:
            await pool.close()

    asyncio.run(_migrate())
    yield


# ── Per-test DB reset ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    """
    Close any existing pool, create a fresh one bound to this test's event
    loop, then truncate all tables.

    Because asyncio_default_fixture_loop_scope = "function", this fixture and
    the test function share the same event loop.  The pool created here is
    therefore always on the correct loop.
    """
    await _db_module.close_pool()
    pool = await _db_module.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE TABLE transactions, quotes, balances, customers
            RESTART IDENTITY CASCADE
            """
        )
    yield
    # Close pool after each test so the next test starts clean.
    await _db_module.close_pool()


# ── Rate provider ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def stable_rates(monkeypatch):
    """Pin rates to seed values — tests never depend on the live API."""
    mids = _compute_all_mids(_FALLBACK_MID)
    monkeypatch.setattr(rate_provider, "_mids", mids)
    monkeypatch.setattr(rate_provider, "_fetched_at", datetime.now(timezone.utc))


# ── HTTP client ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Domain helpers ────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def customer(client) -> dict:
    resp = await client.post(
        "/customers",
        json={"name": "Alice Test", "email": "alice@test.example"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest_asyncio.fixture
async def funded_customer(client, customer) -> dict:
    """Customer pre-loaded with 1,000 USD."""
    await client.post(
        f"/customers/{customer['id']}/balances/credit",
        json={"currency": "USD", "amount": "1000.00"},
    )
    return customer


@pytest_asyncio.fixture
async def pending_quote(client, funded_customer) -> dict:
    """A live USD→KES quote for the funded customer."""
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "100.00",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
