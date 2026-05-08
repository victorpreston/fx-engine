from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

SUPPORTED_CURRENCIES = {"USD", "EUR", "KES", "NGN"}

# ISO 3166-1 alpha-2 — two uppercase letters
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

# E.164-ish phone validation — optional leading +, 7–15 digits
_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


# ── Customer ──────────────────────────────────────────────────────────────────


class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    phone: Optional[str] = Field(
        default=None,
        description="E.164 phone number (e.g. +254712345678)",
        examples=["+254712345678"],
    )
    country: Optional[str] = Field(
        default=None,
        description="ISO 3166-1 alpha-2 country code (e.g. KE)",
        examples=["KE"],
    )

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _PHONE_RE.match(v):
            raise ValueError("phone must be in E.164 format, e.g. +254712345678")
        return v

    @field_validator("country")
    @classmethod
    def validate_country(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.upper()
        if not _COUNTRY_RE.match(v):
            raise ValueError("country must be an ISO 3166-1 alpha-2 code, e.g. KE")
        return v


class CustomerResponse(BaseModel):
    id: UUID
    name: str
    email: str
    phone: Optional[str] = None
    country: Optional[str] = None
    kyc_status: str = "pending"
    created_at: datetime

    model_config = {"from_attributes": True}


class KycStatusUpdate(BaseModel):
    kyc_status: Literal["pending", "verified", "rejected"]


# ── Balances ──────────────────────────────────────────────────────────────────


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


# ── Quotes ────────────────────────────────────────────────────────────────────


class QuoteRequest(BaseModel):
    customer_id: UUID
    from_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    to_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(..., gt=0)
    reference: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Optional client-provided reference for reconciliation (e.g. INV-2026-001)",
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


# ── Transactions ──────────────────────────────────────────────────────────────


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


# ── Rates ─────────────────────────────────────────────────────────────────────


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


# ── Health ────────────────────────────────────────────────────────────────────


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
