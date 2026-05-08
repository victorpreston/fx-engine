"""Tests for quote generation."""
from __future__ import annotations

from decimal import Decimal

import pytest


async def test_create_quote_returns_expected_shape(client, funded_customer):
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "100.00",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["from_currency"] == "USD"
    assert data["to_currency"] == "KES"
    assert Decimal(data["from_amount"]) == Decimal("100.00")
    assert Decimal(data["to_amount"]) > 0
    assert Decimal(data["rate"]) > 0
    assert "quote_id" in data
    assert "expires_at" in data
    assert "created_at" in data


async def test_quote_amount_has_two_decimal_places(client, funded_customer):
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "NGN",
            "amount": "1.00",
        },
    )
    assert resp.status_code == 201
    to_amount = Decimal(resp.json()["to_amount"])
    assert to_amount == to_amount.quantize(Decimal("0.01"))


async def test_quote_rate_is_locked_at_generation_time(client, funded_customer, monkeypatch):
    """Rate stored in the quote must not change when live rates shift."""
    from app.rates import rate_provider
    from datetime import datetime, timezone

    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "EUR",
            "amount": "100.00",
        },
    )
    assert resp.status_code == 201
    quoted_rate = resp.json()["rate"]
    quoted_to_amount = resp.json()["to_amount"]

    # Simulate live rate doubling after the quote was generated.
    new_mids = {k: v * 2 for k, v in rate_provider._mids.items()}
    monkeypatch.setattr(rate_provider, "_mids", new_mids)
    monkeypatch.setattr(rate_provider, "_fetched_at", datetime.now(timezone.utc))

    # Execute uses stored rate — to_amount should not change.
    quote_id = resp.json()["quote_id"]
    exec_resp = await client.post(
        f"/quotes/{quote_id}/execute",
        json={"customer_id": funded_customer["id"]},
    )
    assert exec_resp.status_code == 200
    tx = exec_resp.json()
    assert tx["rate"] == quoted_rate
    assert tx["to_amount"] == quoted_to_amount


async def test_quote_rejects_same_currency(client, funded_customer):
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "USD",
            "amount": "100.00",
        },
    )
    assert resp.status_code == 400


async def test_quote_rejects_zero_amount(client, funded_customer):
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "0",
        },
    )
    assert resp.status_code in (400, 422)


async def test_quote_rejects_negative_amount(client, funded_customer):
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "-50",
        },
    )
    assert resp.status_code in (400, 422)


async def test_quote_rejects_unknown_customer(client):
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": "00000000-0000-0000-0000-000000000000",
            "from_currency": "USD",
            "to_currency": "KES",
            "amount": "100.00",
        },
    )
    assert resp.status_code == 404


async def test_quote_rejects_unsupported_currency(client, funded_customer):
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": "USD",
            "to_currency": "GBP",
            "amount": "100.00",
        },
    )
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize("pair", [
    ("USD", "EUR"), ("USD", "KES"), ("USD", "NGN"),
    ("EUR", "USD"), ("EUR", "KES"), ("EUR", "NGN"),
    ("KES", "USD"), ("KES", "EUR"), ("KES", "NGN"),
    ("NGN", "USD"), ("NGN", "EUR"), ("NGN", "KES"),
])
async def test_all_pairs_produce_positive_quote(client, funded_customer, pair):
    from_ccy, to_ccy = pair
    # Credit the from_currency so the customer has it
    await client.post(
        f"/customers/{funded_customer['id']}/balances/credit",
        json={"currency": from_ccy, "amount": "10000.00"},
    )
    resp = await client.post(
        "/quotes",
        json={
            "customer_id": funded_customer["id"],
            "from_currency": from_ccy,
            "to_currency": to_ccy,
            "amount": "100.00",
        },
    )
    assert resp.status_code == 201, f"Pair {from_ccy}/{to_ccy} failed: {resp.text}"
    assert Decimal(resp.json()["to_amount"]) > 0
