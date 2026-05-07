"""
Concurrency safety tests.

These tests fire N concurrent requests against a single quote and assert
that exactly one succeeds.  They require a real PostgreSQL instance because
they depend on database-level row locking (SELECT ... FOR UPDATE).

The test uses asyncio.gather to send all requests in parallel within the
same event loop, which exercises the PostgreSQL lock wait path.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal


async def test_concurrent_execute_only_one_succeeds(client, funded_customer, pending_quote):
    """
    N simultaneous execute requests for the same quote → exactly one 200,
    all others 409 (already_executed).

    This is the core concurrency safety test.  Without SELECT FOR UPDATE,
    multiple requests would pass the status='pending' check concurrently
    and produce multiple transactions for the same quote.
    """
    N = 20
    quote_id = pending_quote["quote_id"]
    customer_id = funded_customer["id"]

    results = await asyncio.gather(
        *[
            client.post(
                f"/quotes/{quote_id}/execute",
                json={"customer_id": customer_id},
            )
            for _ in range(N)
        ]
    )

    status_codes = [r.status_code for r in results]
    successes = [r for r in results if r.status_code == 200]
    failures = [r for r in results if r.status_code == 409]

    assert len(successes) == 1, (
        f"Expected exactly 1 success, got {len(successes)}.\n"
        f"Status codes: {sorted(status_codes)}"
    )
    assert len(failures) == N - 1

    # Verify database has exactly one transaction for this quote.
    pool = await __import__("app.database", fromlist=["get_pool"]).get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = $1", quote_id
        )
    assert count == 1, f"Expected 1 transaction row, found {count}"


async def test_concurrent_execute_balance_debited_exactly_once(client, funded_customer):
    """
    Balance must be debited exactly once even under concurrent execute.
    """
    initial_credit = Decimal("500.00")
    from_amount = Decimal("100.00")

    await client.post(
        f"/customers/{funded_customer['id']}/balances/credit",
        json={"currency": "USD", "amount": str(initial_credit)},
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
            client.post(
                f"/quotes/{quote_id}/execute",
                json={"customer_id": funded_customer["id"]},
            )
            for _ in range(N)
        ]
    )

    balances_resp = await client.get(f"/customers/{funded_customer['id']}/balances")
    balances = {b["currency"]: Decimal(b["amount"]) for b in balances_resp.json()["balances"]}

    # Total funded was 1000 (fixture) + 500 (above) = 1500 USD.
    # Exactly 100 USD was converted.
    expected_usd = Decimal("1000.00") + initial_credit - from_amount
    assert balances["USD"] == expected_usd, (
        f"Expected USD balance {expected_usd}, got {balances['USD']}"
    )


async def test_different_quotes_execute_concurrently(client, funded_customer):
    """
    Two different quotes can execute concurrently without deadlocking.
    Balance ordering (alphabetical) prevents deadlocks between concurrent
    executions that touch the same pair in opposite directions.
    """
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
        client.post(f"/quotes/{q1['quote_id']}/execute", json={"customer_id": funded_customer["id"]}),
        client.post(f"/quotes/{q2['quote_id']}/execute", json={"customer_id": funded_customer["id"]}),
    )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
