"""
Idempotency tests.

Sequential tests go through the HTTP client (no pool issues).
The concurrent-retry test uses per-task mini-pools for the same reason
as test_concurrency.py — see that module for the full explanation.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.config import settings
from app.engine import fx
from app.engine.fx import _EXECUTE_ENDPOINT, _request_hash
from app.services.database import get_pool, set_type_codecs


async def _mini_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=1,
        command_timeout=30,
        init=set_type_codecs,
    )


async def test_same_idempotency_key_returns_identical_response(
    client, funded_customer, pending_quote
):
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


async def test_idempotent_retry_does_not_debit_twice(
    client, funded_customer, pending_quote
):
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
    balances = {
        b["currency"]: Decimal(b["amount"]) for b in balances_resp.json()["balances"]
    }

    # 1000 USD − 100 USD = 900. Lower means double-debit.
    assert balances["USD"] == Decimal("900.00"), (
        f"USD balance is {balances['USD']} — double-debit occurred"
    )


async def test_different_idempotency_keys_are_independent(client, funded_customer):
    await client.post(
        f"/customers/{funded_customer['id']}/balances/credit",
        json={"currency": "USD", "amount": "1000.00"},
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
                "from_currency": "USD",
                "to_currency": "KES",
                "amount": "50.00",
            },
        )
    ).json()

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


async def test_concurrent_retry_with_same_key_executes_exactly_once(
    funded_customer, pending_quote
):
    """
    Tests two things:

    1. N simultaneous calls with the same idempotency key: because all tasks
       start their transactions before any of them commits, all N see no cached
       entry and race on SELECT FOR UPDATE.  Exactly one succeeds; the rest
       raise QuoteAlreadyExecutedError.  This is the correct behaviour — the
       idempotency guard only short-circuits *retries that arrive after the
       first call completes*, not in-flight duplicates.

    2. A subsequent retry (after the first call completed) returns the exact
       same transaction record as the original — proving the idempotency cache
       works for the common retry-after-timeout client pattern.
    """
    from app.exceptions import ExecutionInProgressError, QuoteAlreadyExecutedError

    key = str(uuid.uuid4())
    quote_id = pending_quote["quote_id"]
    customer_id = funded_customer["id"]
    N = 8

    pools = [await _mini_pool() for _ in range(N)]
    try:
        results = await asyncio.gather(
            *[
                fx.execute_quote(
                    pool=pool,
                    customer_id=customer_id,
                    quote_id=quote_id,
                    idempotency_key=key,
                )
                for pool in pools
            ],
            return_exceptions=True,
        )
    finally:
        for pool in pools:
            await pool.close()

    successes = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]

    # Exactly one concurrent caller wins the FOR UPDATE race.
    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {errors}"
    # Others see either quote_already_executed (lost the FOR UPDATE race) or
    # execution_in_progress (found the idempotency row with completed_at=NULL).
    allowed = (QuoteAlreadyExecutedError, ExecutionInProgressError)
    assert all(isinstance(e, allowed) for e in errors), (
        f"Unexpected error types: {[type(e).__name__ for e in errors]}"
    )

    # Retry after completion must return the exact same transaction.
    pool = await get_pool()
    retry = await fx.execute_quote(
        pool=pool,
        customer_id=customer_id,
        quote_id=quote_id,
        idempotency_key=key,
    )
    assert str(retry["id"]) == str(successes[0]["id"]), (
        "Idempotency replay returned a different transaction"
    )

    # Exactly one row in the DB.
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = $1", quote_id
        )
    assert count == 1, f"Expected 1 transaction row, found {count}"


async def test_in_flight_key_returns_execution_in_progress(
    funded_customer, pending_quote
):
    """A key with completed_at=NULL (in-flight or failed mid-execute) must return 409
    execution_in_progress rather than racing to execute a second time."""
    from app.exceptions import ExecutionInProgressError
    from app.services.database import get_pool

    key = str(uuid.uuid4())
    pool = await get_pool()

    # Manually insert an idempotency row with completed_at=NULL to simulate in-flight.
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO idempotency_keys (endpoint, key, request_hash)
            VALUES ('POST /quotes/{quote_id}/execute', $1, $2)
            """,
            key,
            "deadbeef" * 8,  # dummy hash that matches the key
        )

    # A retry with the same key and the matching hash should get execution_in_progress.
    # To match, we need to insert with the actual hash. Re-insert with correct hash.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM idempotency_keys WHERE key = $1", key)
        real_hash = _request_hash(pending_quote["quote_id"], funded_customer["id"])
        await conn.execute(
            """
            INSERT INTO idempotency_keys (endpoint, key, request_hash)
            VALUES ($1, $2, $3)
            """,
            _EXECUTE_ENDPOINT,
            key,
            real_hash,
        )

    with pytest.raises(ExecutionInProgressError):
        await fx.execute_quote(
            pool=pool,
            customer_id=funded_customer["id"],
            quote_id=pending_quote["quote_id"],
            idempotency_key=key,
        )


async def test_missing_idempotency_key_returns_400(
    client, funded_customer, pending_quote
):
    """Idempotency-Key is required on execute; omitting it must return 400."""
    resp = await client.post(
        f"/quotes/{pending_quote['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        # No Idempotency-Key header
    )
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "idempotency_key_missing"


async def test_same_key_different_quote_returns_409(client, funded_customer):
    """Same Idempotency-Key with a different quote_id must return 409 idempotency_conflict."""
    # Fund extra to allow two quotes.
    await client.post(
        f"/customers/{funded_customer['id']}/balances/credit",
        json={"currency": "USD", "amount": "500.00"},
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
                "from_currency": "USD",
                "to_currency": "KES",
                "amount": "50.00",
            },
        )
    ).json()

    shared_key = str(uuid.uuid4())

    # First execute with q1 — succeeds.
    r1 = await client.post(
        f"/quotes/{q1['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": shared_key},
    )
    assert r1.status_code == 200

    # Second execute with same key but different quote — must be 409.
    r2 = await client.post(
        f"/quotes/{q2['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": shared_key},
    )
    assert r2.status_code == 409
    assert r2.json()["error_code"] == "idempotency_conflict"
