"""
Concurrency safety tests.

Why mini-pools instead of a shared pool
----------------------------------------
asyncpg binds each connection's internal protocol Futures to the event loop
that was running when the connection was established.  When asyncio.gather
spawns N concurrent Tasks that all call pool.acquire() on the same shared
pool, the pool's internal asyncio.Condition and Queue interact with those
Tasks in a way that leaves some connections in a non-IDLE protocol state —
causing InterfaceError("another operation is in progress") even though each
Task holds its own nominally-exclusive connection.

The fix: give each concurrent Task its own single-connection pool.  There is
no shared pool state, so there are no cross-task protocol-state conflicts.
The SELECT … FOR UPDATE locking is still real — the transactions are sent to
the same PostgreSQL server and race against each other exactly as they would
in production.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import asyncpg

from app.config import settings
from app.engine import fx
from app.exceptions import QuoteAlreadyExecutedError
from app.services.database import set_type_codecs


async def _mini_pool() -> asyncpg.Pool:
    """One-connection pool bound to the current running loop."""
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=1,
        command_timeout=30,
        init=set_type_codecs,
    )


async def test_concurrent_execute_only_one_succeeds(funded_customer, pending_quote):
    """
    N simultaneous execute_quote calls on the same quote → exactly 1 succeeds.

    Each task uses its own single-connection pool so there is no shared pool
    state.  The concurrency guarantee (SELECT FOR UPDATE) is still exercised
    at the PostgreSQL level: all N transactions race on the same row lock and
    exactly one wins.
    """
    N = 10
    quote_id = pending_quote["quote_id"]
    customer_id = funded_customer["id"]

    pools = [await _mini_pool() for _ in range(N)]
    try:
        results = await asyncio.gather(
            *[
                fx.execute_quote(
                    pool=pool,
                    customer_id=customer_id,
                    quote_id=quote_id,
                )
                for pool in pools
            ],
            return_exceptions=True,
        )
    finally:
        for pool in pools:
            await pool.close()

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1, (
        f"Expected exactly 1 success, got {len(successes)}.\n"
        f"Failures: {[str(f) for f in failures[:3]]}"
    )
    assert len(failures) == N - 1
    assert all(isinstance(f, QuoteAlreadyExecutedError) for f in failures), (
        f"Unexpected failure types: {[type(f).__name__ for f in failures]}"
    )

    # Exactly one transaction row in the database.
    from app.services.database import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = $1", quote_id
        )
    assert count == 1, f"Expected 1 transaction row, found {count}"


async def test_concurrent_execute_balance_debited_exactly_once(client, funded_customer):
    """Balance must be debited exactly once even under concurrent execute."""
    extra = Decimal("500.00")
    from_amount = Decimal("100.00")

    await client.post(
        f"/customers/{funded_customer['id']}/balances/credit",
        json={"currency": "USD", "amount": str(extra)},
    )
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": str(from_amount),
        },
    )
    assert quote_resp.status_code == 201
    quote_id = quote_resp.json()["quote_id"]

    N = 8
    pools = [await _mini_pool() for _ in range(N)]
    try:
        await asyncio.gather(
            *[
                fx.execute_quote(
                    pool=pool,
                    customer_id=funded_customer["id"],
                    quote_id=quote_id,
                )
                for pool in pools
            ],
            return_exceptions=True,
        )
    finally:
        for pool in pools:
            await pool.close()

    balances_resp = await client.get(f"/customers/{funded_customer['id']}/balances")
    balances = {
        b["currency"]: Decimal(b["amount"]) for b in balances_resp.json()["balances"]
    }

    # Started with 1000 (fixture) + 500 (above) = 1500 USD. Debited exactly 100.
    expected = Decimal("1000.00") + extra - from_amount
    assert balances["USD"] == expected, (
        f"Expected USD={expected}, got {balances['USD']} (double-debit if lower)"
    )


async def test_different_quotes_execute_concurrently_no_deadlock(
    client, funded_customer
):
    """
    Two quotes converting in opposite directions execute concurrently without
    deadlocking.  Alphabetical balance-lock ordering prevents hold-and-wait.
    """
    await client.post(
        f"/customers/{funded_customer['id']}/balances/credit",
        json={"currency": "EUR", "amount": "500.00"},
    )

    q1 = (
        await client.post(
            "/quotes",
            json={
                "customer_id": funded_customer["id"],
                "from_currency": "USD",
                "to_currency": "EUR",
                "amount": "50.00",
            },
        )
    ).json()

    q2 = (
        await client.post(
            "/quotes",
            json={
                "customer_id": funded_customer["id"],
                "from_currency": "EUR",
                "to_currency": "USD",
                "amount": "50.00",
            },
        )
    ).json()

    p1, p2 = await _mini_pool(), await _mini_pool()
    try:
        r1, r2 = await asyncio.gather(
            fx.execute_quote(
                pool=p1, customer_id=funded_customer["id"], quote_id=q1["quote_id"]
            ),
            fx.execute_quote(
                pool=p2, customer_id=funded_customer["id"], quote_id=q2["quote_id"]
            ),
            return_exceptions=True,
        )
    finally:
        await p1.close()
        await p2.close()

    assert not isinstance(r1, Exception), f"q1 failed: {r1}"
    assert not isinstance(r2, Exception), f"q2 failed: {r2}"
