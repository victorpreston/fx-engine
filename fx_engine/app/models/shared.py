from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

# Shared constant — imported by customer and quote models.
SUPPORTED_CURRENCIES = {"USD", "EUR", "KES", "NGN"}


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
