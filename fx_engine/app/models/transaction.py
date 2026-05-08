from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


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
