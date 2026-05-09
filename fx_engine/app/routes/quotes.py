from __future__ import annotations

from decimal import Decimal
from typing import Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException

from app.engine import fx
from app.models.quote import ExecuteRequest, QuoteRequest, QuoteResponse
from app.models.transaction import TransactionResponse
from app.providers.rates import rate_provider
from app.services.database import get_connection, get_pool

router = APIRouter(prefix="/quotes", tags=["quotes"])
log = structlog.get_logger(__name__)


@router.get("", response_model=list[QuoteResponse])
async def list_quotes(
    customer_id: Optional[str] = None,
    conn: asyncpg.Connection = Depends(get_connection),
):
    if customer_id:
        rows = await conn.fetch(
            "SELECT * FROM quotes WHERE customer_id = $1 ORDER BY created_at DESC",
            customer_id,
        )
    else:
        rows = await conn.fetch("SELECT * FROM quotes ORDER BY created_at DESC")

    return [
        {
            "quote_id": r["id"],
            "customer_id": r["customer_id"],
            "from_currency": r["from_currency"],
            "to_currency": r["to_currency"],
            "from_amount": Decimal(str(r["from_amount"])),
            "to_amount": Decimal(str(r["to_amount"])),
            "rate": Decimal(str(r["rate"])),
            "mid_rate": Decimal(str(r["mid_rate"])) if r["mid_rate"] else None,
            "reference": r["reference"],
            "expires_at": r["expires_at"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@router.get("/{quote_id}", response_model=QuoteResponse)
async def get_quote(
    quote_id: str,
    conn: asyncpg.Connection = Depends(get_connection),
):
    row = await conn.fetchrow("SELECT * FROM quotes WHERE id = $1", quote_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    return {
        "quote_id": row["id"],
        "customer_id": row["customer_id"],
        "from_currency": row["from_currency"],
        "to_currency": row["to_currency"],
        "from_amount": Decimal(str(row["from_amount"])),
        "to_amount": Decimal(str(row["to_amount"])),
        "rate": Decimal(str(row["rate"])),
        "mid_rate": Decimal(str(row["mid_rate"])) if row["mid_rate"] else None,
        "reference": row["reference"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
    }


@router.post("", response_model=QuoteResponse, status_code=201)
async def create_quote(
    body: QuoteRequest,
    conn: asyncpg.Connection = Depends(get_connection),
):
    if body.from_currency == body.to_currency:
        raise HTTPException(
            status_code=400, detail="from_currency and to_currency must differ"
        )

    row = await fx.generate_quote(
        conn=conn,
        rate_provider=rate_provider,
        customer_id=str(body.customer_id),
        from_currency=body.from_currency,
        to_currency=body.to_currency,
        from_amount=body.amount,
        reference=body.reference,
    )

    return {
        "quote_id": row["id"],
        "customer_id": row["customer_id"],
        "from_currency": row["from_currency"],
        "to_currency": row["to_currency"],
        "from_amount": Decimal(str(row["from_amount"])),
        "to_amount": Decimal(str(row["to_amount"])),
        "rate": Decimal(str(row["rate"])),
        "mid_rate": Decimal(str(row["mid_rate"])) if row["mid_rate"] else None,
        "reference": row["reference"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
    }


@router.post("/{quote_id}/execute", response_model=TransactionResponse)
async def execute_quote(
    quote_id: str,
    body: ExecuteRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    pool = await get_pool()
    tx = await fx.execute_quote(
        pool=pool,
        customer_id=str(body.customer_id),
        quote_id=quote_id,
        idempotency_key=idempotency_key,
    )

    return {
        "transaction_id": tx["id"],
        "quote_id": tx["quote_id"],
        "customer_id": tx["customer_id"],
        "from_currency": tx["from_currency"],
        "to_currency": tx["to_currency"],
        "from_amount": Decimal(str(tx["from_amount"])),
        "to_amount": Decimal(str(tx["to_amount"])),
        "rate": Decimal(str(tx["rate"])),
        "status": tx["status"],
        "executed_at": tx["executed_at"],
    }
