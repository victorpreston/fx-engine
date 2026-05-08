from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.shared import SUPPORTED_CURRENCIES


class QuoteRequest(BaseModel):
    customer_id: UUID
    from_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    to_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(..., gt=0)
    reference: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Client-provided reference for reconciliation (e.g. INV-2026-001)",
        examples=["INV-2026-001"],
    )

    @field_validator("from_currency", "to_currency")
    @classmethod
    def currency_must_be_supported(cls, v: str) -> str:
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Unsupported currency: {v}. Supported: {sorted(SUPPORTED_CURRENCIES)}"
            )
        return v

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_amount(cls, v):
        return Decimal(str(v))


class QuoteResponse(BaseModel):
    quote_id: UUID
    customer_id: UUID
    from_currency: str
    to_currency: str
    from_amount: Decimal
    to_amount: Decimal
    rate: Decimal
    mid_rate: Optional[Decimal] = None
    reference: Optional[str] = None
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class ExecuteRequest(BaseModel):
    customer_id: UUID
