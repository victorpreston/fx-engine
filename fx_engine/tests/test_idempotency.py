"""
Idempotency tests.

The Idempotency-Key header ensures that client retries do not cause
double execution.  The key lookup and execute happen inside the same
database transaction, preventing TOCTOU races under concurrent retries.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal


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

    await client.post(
        f"/quotes/{quote_id}/execute",
        json={"customer_id": customer_id},
        headers={"Idempotency-Key": key},
    )
    await client.post(
        f"/quotes/{quote_id}/execute",
        json={"customer_id": customer_id},
        headers={"Idempotency-Key": key},
    )

    balances_resp = await client.get(f"/customers/{customer_id}/balances")
    balances = {b["currency"]: Decimal(b["amount"]) for b in balances_resp.json()["balances"]}

    # 1000 USD - 100 USD = 900 USD.  If debited twice it would be 800.
    assert balances["USD"] == Decimal("900.00"), (
        f"USD balance is {balances['USD']} — double-debit occurred"
    )


async def test_different_idempotency_keys_are_independent(client, funded_customer):
    """Two different quotes with different keys must both succeed independently."""
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


async def test_concurrent_retry_with_same_key_executes_exactly_once(client, funded_customer, pending_quote):
    """
    N concurrent retries with the same idempotency key must produce exactly
    one database write.  This tests the TOCTOU safety of the idempotency
    check being inside the same transaction as the execute.
    """
    key = str(uuid.uuid4())
    quote_id = pending_quote["quote_id"]
    customer_id = funded_customer["id"]
    N = 10

    results = await asyncio.gather(
        *[
            client.post(
                f"/quotes/{quote_id}/execute",
                json={"customer_id": customer_id},
                headers={"Idempotency-Key": key},
            )
            for _ in range(N)
        ]
    )

    statuses = [r.status_code for r in results]
    # All responses must be 200 (idempotent replay) — none should be 500.
    assert all(s == 200 for s in statuses), (
        f"Expected all 200, got: {sorted(statuses)}"
    )

    # All responses must return the same transaction_id.
    tx_ids = {r.json()["transaction_id"] for r in results}
    assert len(tx_ids) == 1, f"Got multiple transaction IDs: {tx_ids}"

    # Exactly one row in transactions.
    pool = await __import__("app.database", fromlist=["get_pool"]).get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = $1", quote_id
        )
    assert count == 1
