from __future__ import annotations

from decimal import Decimal

import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException

from app.models.customer import (
    BalanceItem,
    BalancesResponse,
    CreditRequest,
    CustomerCreate,
    CustomerResponse,
    KycStatusUpdate,
)
from app.services.database import get_connection

router = APIRouter(prefix="/customers", tags=["customers"])
log = structlog.get_logger(__name__)


@router.post("", response_model=CustomerResponse, status_code=201)
async def create_customer(
    body: CustomerCreate,
    conn: asyncpg.Connection = Depends(get_connection),
):
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO customers (name, email, phone, country)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            body.name,
            body.email,
            body.phone,
            body.country,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Email already registered")

    log.info(
        "customer_created",
        customer_id=str(row["id"]),
        email=body.email,
        country=body.country,
    )
    return dict(row)


@router.get("/{customer_id}", response_model=CustomerResponse)
async def get_customer(
    customer_id: str,
    conn: asyncpg.Connection = Depends(get_connection),
):
    row = await conn.fetchrow("SELECT * FROM customers WHERE id = $1", customer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return dict(row)


@router.patch("/{customer_id}/kyc", response_model=CustomerResponse)
async def update_kyc_status(
    customer_id: str,
    body: KycStatusUpdate,
    conn: asyncpg.Connection = Depends(get_connection),
):
    """
    Update a customer's KYC verification status.
    In production this endpoint would be restricted to internal/admin roles.
    """
    row = await conn.fetchrow(
        """
        UPDATE customers
           SET kyc_status = $1
         WHERE id = $2
        RETURNING *
        """,
        body.kyc_status,
        customer_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    log.info(
        "kyc_status_updated",
        customer_id=customer_id,
        kyc_status=body.kyc_status,
    )
    return dict(row)


@router.get("/{customer_id}/balances", response_model=BalancesResponse)
async def get_balances(
    customer_id: str,
    conn: asyncpg.Connection = Depends(get_connection),
):
    customer = await conn.fetchrow(
        "SELECT id FROM customers WHERE id = $1", customer_id
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    rows = await conn.fetch(
        "SELECT currency, amount FROM balances WHERE customer_id = $1 ORDER BY currency",
        customer_id,
    )
    return {
        "customer_id": customer_id,
        "balances": [
            {"currency": r["currency"], "amount": Decimal(str(r["amount"]))}
            for r in rows
        ],
    }


@router.post(
    "/{customer_id}/balances/credit", response_model=BalanceItem, status_code=200
)
async def credit_balance(
    customer_id: str,
    body: CreditRequest,
    conn: asyncpg.Connection = Depends(get_connection),
):
    """
    Credit a customer's balance in a given currency.
    Intended as an internal funding endpoint; in production this should
    be protected behind authentication or restricted to internal networks.
    """
    customer = await conn.fetchrow(
        "SELECT id FROM customers WHERE id = $1", customer_id
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    row = await conn.fetchrow(
        """
        INSERT INTO balances (customer_id, currency, amount)
        VALUES ($1, $2, $3)
        ON CONFLICT (customer_id, currency)
        DO UPDATE SET amount = balances.amount + EXCLUDED.amount, updated_at = NOW()
        RETURNING currency, amount
        """,
        customer_id,
        body.currency,
        str(body.amount),
    )
    log.info(
        "balance_credited",
        customer_id=customer_id,
        currency=body.currency,
        amount=str(body.amount),
    )
    return {"currency": row["currency"], "amount": Decimal(str(row["amount"]))}
