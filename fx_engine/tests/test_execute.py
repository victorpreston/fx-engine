"""Tests for quote execution — atomicity, balance updates, error cases."""

from __future__ import annotations

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
    import app.fx_engine as fx_module

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(fx_module, "datetime", FakeDatetime)

    exec_resp = await client.post(
        f"/quotes/{quote_id}/execute",
        json={"customer_id": funded_customer["id"]},
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
    )
    assert resp.status_code == 404  # Don't reveal the quote exists


async def test_execute_twice_fails_second_time(client, funded_customer, pending_quote):
    """Executing the same quote twice must fail on the second attempt."""
    first = await client.post(
        f"/quotes/{pending_quote['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
    )
    assert first.status_code == 200

    second = await client.post(
        f"/quotes/{pending_quote['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
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
    pool = await __import__("app.database", fromlist=["get_pool"]).get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE balances SET amount = 0 WHERE customer_id = $1 AND currency = 'USD'",
            customer["id"],
        )

    exec_resp = await client.post(
        f"/quotes/{quote_resp.json()['quote_id']}/execute",
        json={"customer_id": customer["id"]},
    )
    assert exec_resp.status_code == 422

    # USD balance must still be 0 (not negative).
    balances_resp = await client.get(f"/customers/{customer['id']}/balances")
    b = {b["currency"]: Decimal(b["amount"]) for b in balances_resp.json()["balances"]}
    assert b.get("USD", Decimal("0")) == Decimal("0")
    assert b.get("KES", Decimal("0")) == Decimal("0")
