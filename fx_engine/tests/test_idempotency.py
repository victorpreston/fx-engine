"""
Idempotency tests.

Sequential tests use the HTTP client.  The concurrent-retry test calls
execute_quote() directly to avoid the asyncpg event-loop binding issue
that arises when asyncio.gather + ASGITransport are combined (see
test_concurrency.py for a full explanation).
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from app import fx_engine
from app.database import get_pool


async def test_same_idempotency_key_returns_identical_response(client, funded_customer, pending_quote):
    key = str(uuid.uuid4())
    quote_id = pending_quote["quote_id"]
    customer_id = funded_customer["id"]

    first = await client.post(
        f"/quotes/{quote_id}/execute",
        json={"customer_id": customer_id},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 200

    second = await client.post(
        f"/quotes/{quote_id}/execute",
        json={"customer_id": customer_id},
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 200
    assert first.json()["transaction_id"] == second.json()["transaction_id"]


async def test_idempotent_retry_does_not_debit_twice(client, funded_customer, pending_quote):
    key = str(uuid.uuid4())
    quote_id = pending_quote["quote_id"]
    customer_id = funded_customer["id"]

    for _ in range(3):
        await client.post(
            f"/quotes/{quote_id}/execute",
            json={"customer_id": customer_id},
            headers={"Idempotency-Key": key},
        )

    balances_resp = await client.get(f"/customers/{customer_id}/balances")
    balances = {b["currency"]: Decimal(b["amount"]) for b in balances_resp.json()["balances"]}

    # 1000 USD - 100 USD = 900. If debited more than once it would be lower.
    assert balances["USD"] == Decimal("900.00"), (
        f"USD balance is {balances['USD']} — double-debit occurred"
    )


async def test_different_idempotency_keys_are_independent(client, funded_customer):
    await client.post(
        f"/customers/{funded_customer['id']}/balances/credit",
        json={"currency": "USD", "amount": "1000.00"},
    )

    q1 = (await client.post(
        "/quotes",
        json={"customer_id": funded_customer["id"], "from_currency": "USD",
              "to_currency": "EUR", "amount": "50.00"},
    )).json()
    q2 = (await client.post(
        "/quotes",
        json={"customer_id": funded_customer["id"], "from_currency": "USD",
              "to_currency": "KES", "amount": "50.00"},
    )).json()

    r1 = await client.post(
        f"/quotes/{q1['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    r2 = await client.post(
        f"/quotes/{q2['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["transaction_id"] != r2.json()["transaction_id"]


async def test_concurrent_retry_with_same_key_executes_exactly_once(funded_customer, pending_quote):
    """
    N concurrent retries with the same idempotency key must produce exactly
    one database write.

    Calls execute_quote() directly to avoid asyncpg event-loop issues with
    concurrent ASGITransport requests (same reasoning as test_concurrency.py).
    """
    key = str(uuid.uuid4())
    quote_id = pending_quote["quote_id"]
    customer_id = funded_customer["id"]
    pool = await get_pool()
    N = 10

    results = await asyncio.gather(
        *[
            fx_engine.execute_quote(
                pool=pool,
                customer_id=customer_id,
                quote_id=quote_id,
                idempotency_key=key,
            )
            for _ in range(N)
        ],
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Unexpected errors: {[str(e) for e in errors]}"

    # All results must be the same transaction record.
    tx_ids = {str(r["id"]) for r in results}
    assert len(tx_ids) == 1, f"Got multiple transaction IDs: {tx_ids}"

    # Exactly one row in the DB.
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = $1", quote_id
        )
    assert count == 1, f"Expected 1 transaction row, found {count}"
