"""Tests for quote execution — atomicity, balance updates, error cases."""

from __future__ import annotations

import uuid
from decimal import Decimal


async def test_execute_debits_source_and_credits_destination(
    client, funded_customer, pending_quote
):
    # Get balances before.
    before = await client.get(f"/customers/{funded_customer['id']}/balances")
    before_usd = next(
        (
            Decimal(b["amount"])
            for b in before.json()["balances"]
            if b["currency"] == "USD"
        ),
        Decimal("0"),
    )

    resp = await client.post(
        f"/quotes/{pending_quote['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200
    tx = resp.json()
    assert tx["status"] == "success"
    assert tx["from_currency"] == "USD"
    assert tx["to_currency"] == "KES"

    # Verify balances after.
    after = await client.get(f"/customers/{funded_customer['id']}/balances")
    balances = {b["currency"]: Decimal(b["amount"]) for b in after.json()["balances"]}

    expected_from = before_usd - Decimal(pending_quote["from_amount"])
    assert balances["USD"] == expected_from
    assert balances.get("KES", Decimal("0")) == Decimal(pending_quote["to_amount"])


async def test_execute_fails_on_expired_quote(client, funded_customer, monkeypatch):
    """A quote whose TTL has passed must be rejected."""
    from datetime import datetime, timedelta, timezone

    from app.config import settings

    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "50.00",
        },
    )
    assert resp.status_code == 201
    quote_id = resp.json()["quote_id"]

    # Wind time forward past TTL by patching datetime inside fx_engine.
    future = datetime.now(timezone.utc) + timedelta(
        seconds=settings.quote_ttl_seconds + 5
    )
    import app.engine.fx as fx_module

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(fx_module, "datetime", FakeDatetime)

    exec_resp = await client.post(
        f"/quotes/{quote_id}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert exec_resp.status_code == 409
    assert "expired" in exec_resp.json()["error_code"]


async def test_execute_fails_on_insufficient_balance(client, customer):
    """Execute must fail if customer lacks funds; balances must not change."""
    # No balance credited — customer has zero.
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": customer["id"],
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "100.00",
        },
    )
    assert resp.status_code == 201
    quote_id = resp.json()["quote_id"]

    exec_resp = await client.post(
        f"/quotes/{quote_id}/execute",
        json={"customer_id": customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert exec_resp.status_code == 422
    assert "insufficient" in exec_resp.json()["error_code"]

    # Quote must remain pending (not executed, not debited).
    after = await client.get(f"/customers/{customer['id']}/balances")
    balances = {b["currency"]: Decimal(b["amount"]) for b in after.json()["balances"]}
    assert balances.get("USD", Decimal("0")) == Decimal("0")


async def test_execute_fails_on_unknown_quote(client, funded_customer):
    resp = await client.post(
        "/quotes/00000000-0000-0000-0000-000000000000/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


async def test_execute_fails_on_wrong_customer(client, client2=None):
    """A customer cannot execute another customer's quote."""
    # Create two customers
    c1 = (
        await client.post("/customers", json={"name": "C1", "email": "c1@test.example"})
    ).json()
    c2 = (
        await client.post("/customers", json={"name": "C2", "email": "c2@test.example"})
    ).json()

    await client.post(
        f"/customers/{c1['id']}/balances/credit",
        json={"currency": "USD", "amount": "500.00"},
    )
    quote = (
        await client.post(
            "/quotes",
            json={
                "customer_id": c1["id"],
                "from_currency": "USD",
                "to_currency": "EUR",
                "amount": "50.00",
            },
        )
    ).json()

    resp = await client.post(
        f"/quotes/{quote['quote_id']}/execute",
        json={"customer_id": c2["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 404  # Don't reveal the quote exists


async def test_execute_twice_fails_second_time(client, funded_customer, pending_quote):
    """Executing the same quote twice must fail on the second attempt."""
    first = await client.post(
        f"/quotes/{pending_quote['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert first.status_code == 200

    second = await client.post(
        f"/quotes/{pending_quote['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert second.status_code == 409
    assert "already_executed" in second.json()["error_code"]


async def test_execute_atomicity_second_leg_negative_balance_rolls_back(
    client, customer
):
    """
    If the destination credit would not happen (e.g. balance constraint violation),
    the debit must also roll back.  We test this by creating a scenario where the
    from_currency debit would succeed but verifying the whole transaction rolls back
    on insufficient funds.
    """
    # Fund just enough for the from_amount.
    await client.post(
        f"/customers/{customer['id']}/balances/credit",
        json={"currency": "USD", "amount": "100.00"},
    )
    quote_resp = await client.post(
        "/quotes",
        json={
            "customer_id": customer["id"],
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "100.00",
        },
    )
    assert quote_resp.status_code == 201

    # Drain the USD balance so execute fails.
    pool = await __import__("app.services.database", fromlist=["get_pool"]).get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE balances SET amount = 0 WHERE customer_id = $1 AND currency = 'USD'",
            customer["id"],
        )

    exec_resp = await client.post(
        f"/quotes/{quote_resp.json()['quote_id']}/execute",
        json={"customer_id": customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert exec_resp.status_code == 422

    # USD balance must still be 0 (not negative).
    balances_resp = await client.get(f"/customers/{customer['id']}/balances")
    b = {b["currency"]: Decimal(b["amount"]) for b in balances_resp.json()["balances"]}
    assert b.get("USD", Decimal("0")) == Decimal("0")
    assert b.get("KES", Decimal("0")) == Decimal("0")


async def test_atomicity_rollback_via_hook(funded_customer, pending_quote, monkeypatch):
    """
    Prove that a failure between debit and credit rolls back the entire
    transaction atomically: balances unchanged, no transaction row, no ledger
    rows.

    _after_debit_hook() is a no-op in production. Monkeypatching it here
    injects a failure at the exact point between the debit UPDATE and the
    credit UPDATE inside the database transaction.
    """
    import app.engine.fx as fx_module
    from app.services.database import get_pool

    monkeypatch.setattr(
        fx_module,
        "_after_debit_hook",
        lambda: (_ for _ in ()).throw(RuntimeError("injected mid-execute failure")),
    )

    pool = await get_pool()
    customer_id = funded_customer["id"]
    quote_id = pending_quote["quote_id"]

    # Execute must fail.
    try:
        await fx_module.execute_quote(
            pool=pool,
            customer_id=customer_id,
            quote_id=quote_id,
            idempotency_key=str(uuid.uuid4()),
        )
        assert False, "Should have raised"
    except RuntimeError:
        pass

    async with pool.acquire() as conn:
        # Balance unchanged — USD still 1000.
        usd = await conn.fetchval(
            "SELECT amount FROM balances WHERE customer_id = $1 AND currency = 'USD'",
            customer_id,
        )
        assert Decimal(str(usd)) == Decimal("1000.00"), f"USD balance mutated to {usd}"

        # No KES balance row created (credit never happened).
        kes = await conn.fetchval(
            "SELECT amount FROM balances WHERE customer_id = $1 AND currency = 'KES'",
            customer_id,
        )
        assert kes is None or Decimal(str(kes)) == Decimal("0")

        # No transaction row written.
        tx_count = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE quote_id = $1", quote_id
        )
        assert tx_count == 0, f"Expected 0 transaction rows, found {tx_count}"

        # No execution ledger entries written (credit_adjustment rows from
        # the funded_customer fixture exist but must not be touched by rollback).
        ledger_count = await conn.fetchval(
            "SELECT COUNT(*) FROM ledger_entries WHERE customer_id = $1 AND reference_type = 'execution'",
            customer_id,
        )
        assert ledger_count == 0, (
            f"Expected 0 execution ledger rows, found {ledger_count}"
        )

        # Quote is still pending.
        status = await conn.fetchval(
            "SELECT status FROM quotes WHERE id = $1", quote_id
        )
        assert status == "pending", f"Quote status changed to {status}"


async def test_ledger_balance_invariant_after_execute(
    client, funded_customer, pending_quote
):
    """
    After a successful execute, balance == SUM(credits) - SUM(debits)
    per currency for this customer.  This asserts the double-entry
    ledger stays consistent with the materialized balances cache.
    """
    from app.services.database import get_pool

    exec_r = await client.post(
        f"/quotes/{pending_quote['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert exec_r.status_code == 200

    pool = await get_pool()
    customer_id = funded_customer["id"]

    async with pool.acquire() as conn:
        balance_rows = await conn.fetch(
            "SELECT currency, amount FROM balances WHERE customer_id = $1",
            customer_id,
        )
        for row in balance_rows:
            ccy = row["currency"]
            cached_balance = Decimal(str(row["amount"]))

            ledger_balance = await conn.fetchval(
                """
                SELECT COALESCE(
                    SUM(CASE WHEN direction = 'credit' THEN amount ELSE -amount END),
                    0
                )
                FROM ledger_entries
                WHERE customer_id = $1 AND currency = $2
                """,
                customer_id,
                ccy,
            )
            assert Decimal(str(ledger_balance)) == cached_balance, (
                f"{ccy}: cached balance {cached_balance} != ledger sum {ledger_balance}"
            )
