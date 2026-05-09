from __future__ import annotations

from decimal import Decimal
from typing import Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException

from app.models.transaction import TransactionResponse
from app.services.database import get_connection

router = APIRouter(prefix="/transactions", tags=["transactions"])
log = structlog.get_logger(__name__)


@router.get("", response_model=list[TransactionResponse])
async def list_transactions(
    customer_id: Optional[str] = None,
    conn: asyncpg.Connection = Depends(get_connection),
):
    if customer_id:
        rows = await conn.fetch(
            "SELECT * FROM transactions WHERE customer_id = $1 ORDER BY executed_at DESC",
            customer_id,
        )
    else:
        rows = await conn.fetch("SELECT * FROM transactions ORDER BY executed_at DESC")

    return [
        {
            "transaction_id": r["id"],
            "quote_id": r["quote_id"],
            "customer_id": r["customer_id"],
            "from_currency": r["from_currency"],
            "to_currency": r["to_currency"],
            "from_amount": Decimal(str(r["from_amount"])),
            "to_amount": Decimal(str(r["to_amount"])),
            "rate": Decimal(str(r["rate"])),
            "status": r["status"],
            "executed_at": r["executed_at"],
        }
        for r in rows
    ]


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: str,
    conn: asyncpg.Connection = Depends(get_connection),
):
    row = await conn.fetchrow(
        "SELECT * FROM transactions WHERE id = $1", transaction_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {
        "transaction_id": row["id"],
        "quote_id": row["quote_id"],
        "customer_id": row["customer_id"],
        "from_currency": row["from_currency"],
        "to_currency": row["to_currency"],
        "from_amount": Decimal(str(row["from_amount"])),
        "to_amount": Decimal(str(row["to_amount"])),
        "rate": Decimal(str(row["rate"])),
        "status": row["status"],
        "executed_at": row["executed_at"],
    }
