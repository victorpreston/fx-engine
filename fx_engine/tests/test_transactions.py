"""Tests for GET /transactions and GET /transactions/{id}."""

from __future__ import annotations

import uuid
from decimal import Decimal

# ── helpers ───────────────────────────────────────────────────────────────────


async def _execute_quote(client, funded_customer, pending_quote) -> dict:
    resp = await client.post(
        f"/quotes/{pending_quote['quote_id']}/execute",
        json={"customer_id": funded_customer["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── GET /transactions ─────────────────────────────────────────────────────────


async def test_list_transactions_empty(client):
    resp = await client.get("/transactions")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_transactions_returns_executed_transaction(
    client, funded_customer, pending_quote
):
    tx = await _execute_quote(client, funded_customer, pending_quote)

    resp = await client.get("/transactions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["transaction_id"] == tx["transaction_id"]


async def test_list_transactions_contains_expected_fields(
    client, funded_customer, pending_quote
):
    tx = await _execute_quote(client, funded_customer, pending_quote)

    resp = await client.get("/transactions")
    item = resp.json()[0]

    assert item["transaction_id"] == tx["transaction_id"]
    assert item["quote_id"] == pending_quote["quote_id"]
    assert item["customer_id"] == funded_customer["id"]
    assert item["from_currency"] == "USD"
    assert item["to_currency"] == "KES"
    assert Decimal(item["from_amount"]) == Decimal(pending_quote["from_amount"])
    assert Decimal(item["to_amount"]) == Decimal(pending_quote["to_amount"])
    assert item["status"] == "success"
    assert "executed_at" in item


async def test_list_transactions_filter_by_customer_id(client):
    # Create two separate customers, each executing their own quote.
    r1 = await client.post(
        "/customers", json={"name": "Customer One", "email": "c1@tx.example"}
    )
    r2 = await client.post(
        "/customers", json={"name": "Customer Two", "email": "c2@tx.example"}
    )
    c1, c2 = r1.json(), r2.json()

    for customer in (c1, c2):
        await client.post(
            f"/customers/{customer['id']}/balances/credit",
            json={"currency": "USD", "amount": "500.00"},
        )
        q = await client.post(
            "/quotes",
            json={
                "customer_id": customer["id"],
                "from_currency": "USD",
                "to_currency": "EUR",
                "amount": "50.00",
            },
        )
        await client.post(
            f"/quotes/{q.json()['quote_id']}/execute",
            json={"customer_id": customer["id"]},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    resp = await client.get(f"/transactions?customer_id={c1['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["customer_id"] == c1["id"]


async def test_list_transactions_filter_returns_empty_for_unknown_customer(client):
    resp = await client.get(
        "/transactions?customer_id=00000000-0000-0000-0000-000000000000"
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_transactions_ordered_most_recent_first(client):
    r = await client.post(
        "/customers", json={"name": "Multi Tx", "email": "multi@tx.example"}
    )
    cid = r.json()["id"]
    await client.post(
        f"/customers/{cid}/balances/credit",
        json={"currency": "USD", "amount": "1000.00"},
    )

    tx_ids = []
    for _ in range(3):
        q = await client.post(
            "/quotes",
            json={
                "customer_id": cid,
                "from_currency": "USD",
                "to_currency": "EUR",
                "amount": "10.00",
            },
        )
        ex = await client.post(
            f"/quotes/{q.json()['quote_id']}/execute",
            json={"customer_id": cid},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        tx_ids.append(ex.json()["transaction_id"])

    resp = await client.get("/transactions")
    ids_returned = [t["transaction_id"] for t in resp.json()]
    # Most-recent first — last executed should appear first.
    assert ids_returned[0] == tx_ids[-1]


# ── GET /transactions/{id} ────────────────────────────────────────────────────


async def test_get_transaction_by_id(client, funded_customer, pending_quote):
    tx = await _execute_quote(client, funded_customer, pending_quote)

    resp = await client.get(f"/transactions/{tx['transaction_id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["transaction_id"] == tx["transaction_id"]
    assert data["quote_id"] == tx["quote_id"]
    assert data["customer_id"] == tx["customer_id"]
    assert data["status"] == "success"


async def test_get_transaction_amounts_match_quote(
    client, funded_customer, pending_quote
):
    tx = await _execute_quote(client, funded_customer, pending_quote)

    resp = await client.get(f"/transactions/{tx['transaction_id']}")
    data = resp.json()

    assert Decimal(data["from_amount"]) == Decimal(pending_quote["from_amount"])
    assert Decimal(data["to_amount"]) == Decimal(pending_quote["to_amount"])
    assert Decimal(data["rate"]) == Decimal(pending_quote["rate"])


async def test_get_transaction_not_found_returns_404(client):
    resp = await client.get("/transactions/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
