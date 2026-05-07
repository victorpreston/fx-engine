"""
Test fixtures.

Requires a running PostgreSQL instance.  Set TEST_DATABASE_URL in your
environment or .env.test file, or spin up the test compose service:

    docker compose -f docker-compose.test.yml up -d
    TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db pytest
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

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

from app.config import settings  # noqa: E402 — must come after env override
from app.database import close_pool, get_pool, run_migrations  # noqa: E402
from app.main import app  # noqa: E402
from app.rates import rate_provider, _FALLBACK_MID, _compute_all_mids  # noqa: E402


# ── DB-level fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def db_schema():
    """Create schema once per test session."""
    pool = await get_pool()
    await run_migrations(pool)
    yield
    await close_pool()


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    """Truncate all tables before each test in reverse-dependency order."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE TABLE transactions, quotes, balances, customers
            RESTART IDENTITY CASCADE
            """
        )
    yield


# ── Rate provider fixture ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def stable_rates(monkeypatch):
    """Pin rates to fallback values so tests don't depend on live API."""
    mids = _compute_all_mids(_FALLBACK_MID)
    from datetime import datetime, timezone
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
    """A customer pre-loaded with 1,000 USD."""
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
