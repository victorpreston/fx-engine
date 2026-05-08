"""Tests for customer management and balance operations."""

from __future__ import annotations

from decimal import Decimal


async def test_create_customer(client):
    resp = await client.post(
        "/customers",
        json={"name": "Bob Builder", "email": "bob@build.example"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Bob Builder"
    assert data["email"] == "bob@build.example"
    assert "id" in data
    assert "created_at" in data


async def test_duplicate_email_returns_409(client):
    payload = {"name": "Eve A", "email": "eve@dup.example"}
    assert (await client.post("/customers", json=payload)).status_code == 201
    resp = await client.post("/customers", json=payload)
    assert resp.status_code == 409


async def test_get_customer(client, customer):
    resp = await client.get(f"/customers/{customer['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == customer["id"]


async def test_get_nonexistent_customer_returns_404(client):
    resp = await client.get("/customers/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_credit_balance(client, customer):
    resp = await client.post(
        f"/customers/{customer['id']}/balances/credit",
        json={"currency": "USD", "amount": "250.00"},
    )
    assert resp.status_code == 200
    assert Decimal(resp.json()["amount"]) == Decimal("250.00")


async def test_credit_accumulates(client, customer):
    cid = customer["id"]
    await client.post(
        f"/customers/{cid}/balances/credit",
        json={"currency": "KES", "amount": "1000.00"},
    )
    await client.post(
        f"/customers/{cid}/balances/credit",
        json={"currency": "KES", "amount": "500.00"},
    )
    resp = await client.get(f"/customers/{cid}/balances")
    balances = {b["currency"]: Decimal(b["amount"]) for b in resp.json()["balances"]}
    assert balances["KES"] == Decimal("1500.00")


async def test_get_balances_empty(client, customer):
    resp = await client.get(f"/customers/{customer['id']}/balances")
    assert resp.status_code == 200
    assert resp.json()["balances"] == []


async def test_credit_rejects_unsupported_currency(client, customer):
    resp = await client.post(
        f"/customers/{customer['id']}/balances/credit",
        json={"currency": "GBP", "amount": "100.00"},
    )
    assert resp.status_code == 422


async def test_credit_rejects_negative_amount(client, customer):
    resp = await client.post(
        f"/customers/{customer['id']}/balances/credit",
        json={"currency": "USD", "amount": "-50.00"},
    )
    assert resp.status_code == 422
