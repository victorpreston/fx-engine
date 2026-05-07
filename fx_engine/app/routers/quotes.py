from __future__ import annotations

from decimal import Decimal
from typing import Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import fx_engine
from app.database import get_connection, get_pool
from app.exceptions import FXError
from app.rates import rate_provider
from app.schemas import ExecuteRequest, QuoteRequest, QuoteResponse, TransactionResponse

router = APIRouter(prefix="/quotes", tags=["quotes"])
log = structlog.get_logger(__name__)


def _fx_error_to_http(exc: FXError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("", response_model=QuoteResponse, status_code=201)
async def create_quote(
    body: QuoteRequest,
    conn: asyncpg.Connection = Depends(get_connection),
):
    if body.from_currency == body.to_currency:
        raise HTTPException(status_code=400, detail="from_currency and to_currency must differ")

    try:
        row = await fx_engine.generate_quote(
            conn=conn,
            rate_provider=rate_provider,
            customer_id=str(body.customer_id),
            from_currency=body.from_currency,
            to_currency=body.to_currency,
            from_amount=body.amount,
        )
    except FXError as exc:
        raise _fx_error_to_http(exc)

    return {
        "quote_id": row["id"],
        "customer_id": row["customer_id"],
        "from_currency": row["from_currency"],
        "to_currency": row["to_currency"],
        "from_amount": Decimal(str(row["from_amount"])),
        "to_amount": Decimal(str(row["to_amount"])),
        "rate": Decimal(str(row["rate"])),
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
    try:
        tx = await fx_engine.execute_quote(
            pool=pool,
            customer_id=str(body.customer_id),
            quote_id=quote_id,
            idempotency_key=idempotency_key,
        )
    except FXError as exc:
        raise _fx_error_to_http(exc)

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
