from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

SUPPORTED_CURRENCIES = {"USD", "EUR", "KES", "NGN"}


class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr


class CustomerResponse(BaseModel):
    id: UUID
    name: str
    email: str
    created_at: datetime

    model_config = {"from_attributes": True}


class BalanceItem(BaseModel):
    currency: str
    amount: Decimal

    model_config = {"from_attributes": True}


class BalancesResponse(BaseModel):
    customer_id: UUID
    balances: list[BalanceItem]


class CreditRequest(BaseModel):
    currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(..., gt=0)

    @field_validator("currency")
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


class QuoteRequest(BaseModel):
    customer_id: UUID
    from_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    to_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(..., gt=0)

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
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class ExecuteRequest(BaseModel):
    customer_id: UUID


class TransactionResponse(BaseModel):
    transaction_id: UUID
    quote_id: UUID
    customer_id: UUID
    from_currency: str
    to_currency: str
    from_amount: Decimal
    to_amount: Decimal
    rate: Decimal
    status: str
    executed_at: datetime

    model_config = {"from_attributes": True}


class RatePairDetail(BaseModel):
    mid: str
    buy: str
    sell: str
    spread_pct: str


class RatesResponse(BaseModel):
    pairs: dict[str, RatePairDetail]
    last_updated: Optional[datetime]
    source: str
    is_stale: bool


class RefreshResponse(BaseModel):
    status: str
    updated_at: Optional[datetime]
    pairs_loaded: int
    message: Optional[str] = None


class HealthComponent(BaseModel):
    status: str
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    components: dict[str, HealthComponent]
    version: str = "1.0.0"


class ErrorResponse(BaseModel):
    error: str
    error_code: str
    request_id: Optional[str] = None
    detail: Optional[str] = None
