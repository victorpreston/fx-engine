"""
Concurrency safety tests.

These tests call fx_engine.execute_quote() directly with asyncio.gather
rather than through the HTTP layer.  This is intentional: what we are
proving is that the database-level SELECT … FOR UPDATE guarantee holds
when N concurrent callers race on the same quote.  Going through
ASGITransport would test the HTTP stack, not the concurrency invariant,
and introduces event-loop/asyncpg-pool binding issues under concurrent
asyncio tasks.

All tests require a real PostgreSQL instance.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from app import fx_engine
from app.database import get_pool
from app.exceptions import (
    InsufficientBalanceError,
    QuoteAlreadyExecutedError,
)


async def test_concurrent_execute_only_one_succeeds(funded_customer, pending_quote):
    """
    N simultaneous execute_quote calls for the same quote → exactly 1 succeeds.

    Without SELECT FOR UPDATE, multiple callers would all see status='pending',
    all proceed, and produce N transaction rows for one quote.
    """
    N = 20
    pool = await get_pool()
    quote_id = pending_quote["quote_id"]
    customer_id = funded_customer["id"]

    results = await asyncio.gather(
        *[
            fx_engine.execute_quote(
                pool=pool,
                customer_id=customer_id,
                quote_id=quote_id,
            )
            for _ in range(N)
        ],
        return_exceptions=True,
    )

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

    # Exactly one transaction row must exist in the database.
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = $1", quote_id
        )
    assert count == 1, f"Expected 1 transaction row, found {count}"


async def test_concurrent_execute_balance_debited_exactly_once(client, funded_customer):
    """
    Balance must be debited exactly once even under concurrent execute.
    """
    pool = await get_pool()
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

    N = 15
    await asyncio.gather(
        *[
            fx_engine.execute_quote(
                pool=pool,
                customer_id=funded_customer["id"],
                quote_id=quote_id,
            )
            for _ in range(N)
        ],
        return_exceptions=True,
    )

    balances_resp = await client.get(f"/customers/{funded_customer['id']}/balances")
    balances = {b["currency"]: Decimal(b["amount"]) for b in balances_resp.json()["balances"]}

    # Started with 1000 (fixture) + 500 (above) = 1500 USD. Debited exactly 100.
    expected_usd = Decimal("1000.00") + extra - from_amount
    assert balances["USD"] == expected_usd, (
        f"Expected USD balance {expected_usd}, got {balances['USD']} "
        f"(double-debit occurred if lower)"
    )


async def test_different_quotes_execute_concurrently_no_deadlock(client, funded_customer):
    """
    Two quotes converting in opposite directions execute concurrently without
    deadlocking.  The alphabetical balance-lock ordering prevents deadlock.
    """
    pool = await get_pool()

    await client.post(
        f"/customers/{funded_customer['id']}/balances/credit",
        json={"currency": "EUR", "amount": "500.00"},
    )

    q1 = (await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "50.00",
        },
    )).json()

    q2 = (await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "EUR",
            "to_currency": "USD",
            "amount": "50.00",
        },
    )).json()

    r1, r2 = await asyncio.gather(
        fx_engine.execute_quote(pool=pool, customer_id=funded_customer["id"], quote_id=q1["quote_id"]),
        fx_engine.execute_quote(pool=pool, customer_id=funded_customer["id"], quote_id=q2["quote_id"]),
        return_exceptions=True,
    )

    assert not isinstance(r1, Exception), f"q1 failed: {r1}"
    assert not isinstance(r2, Exception), f"q2 failed: {r2}"
