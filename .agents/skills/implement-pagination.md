# Skill: Add Cursor-Based Pagination to a List Endpoint

You are working on the FX Engine. The task is to add paginated list endpoints
— for example, `GET /customers/{id}/transactions` for account statement queries.

## Why cursor-based, not offset

Offset pagination (`LIMIT 20 OFFSET 100`) breaks under concurrent inserts:
a new row shifts every subsequent page. Cursor pagination uses a stable column
value (typically `executed_at` + `id`) as the bookmark. The query is:

```sql
WHERE (executed_at, id) < ($cursor_time, $cursor_id)
ORDER BY executed_at DESC, id DESC
LIMIT $page_size
```

This is stable, index-friendly, and handles concurrent inserts correctly.

## app/models/transaction.py — add pagination models

```python
from typing import Optional

class TransactionPage(BaseModel):
    items: list[TransactionResponse]
    next_cursor: Optional[str] = None  # base64-encoded "timestamp:uuid"
    has_more: bool


class PaginationParams(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: Optional[str] = None  # from previous response's next_cursor
```

## Cursor encoding

```python
import base64, json
from datetime import datetime

def encode_cursor(executed_at: datetime, tx_id: str) -> str:
    raw = json.dumps({"t": executed_at.isoformat(), "id": tx_id})
    return base64.urlsafe_b64encode(raw.encode()).decode()

def decode_cursor(cursor: str) -> tuple[datetime, str]:
    raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    return datetime.fromisoformat(raw["t"]), raw["id"]
```

## app/routes/customers.py — add transaction history endpoint

```python
@router.get("/{customer_id}/transactions", response_model=TransactionPage)
async def get_transactions(
    customer_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    conn: asyncpg.Connection = Depends(get_connection),
):
    customer = await conn.fetchrow("SELECT id FROM customers WHERE id=$1", customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    if cursor:
        cursor_time, cursor_id = decode_cursor(cursor)
        rows = await conn.fetch(
            """
            SELECT * FROM transactions
            WHERE customer_id = $1
              AND (executed_at, id) < ($2, $3::uuid)
            ORDER BY executed_at DESC, id DESC
            LIMIT $4
            """,
            customer_id, cursor_time, cursor_id, limit + 1,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT * FROM transactions
            WHERE customer_id = $1
            ORDER BY executed_at DESC, id DESC
            LIMIT $2
            """,
            customer_id, limit + 1,
        )

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1]["executed_at"], str(items[-1]["id"])) if has_more else None

    return {"items": [dict(r) for r in items], "next_cursor": next_cursor, "has_more": has_more}
```

## Index required

The query pattern `(customer_id, executed_at DESC, id DESC)` is covered by
`idx_transactions_customer_date` which was added in migration `1076c0e1bb3f`.
No additional index is needed.

## Tests to write

```python
async def test_transaction_pagination_returns_correct_pages():
    # Create 25 transactions for the same customer
    # Fetch page 1 (limit=10) — assert 10 items, has_more=True, next_cursor set
    # Fetch page 2 using next_cursor — assert 10 items
    # Fetch page 3 — assert 5 items, has_more=False, next_cursor=None
    # Assert no duplicates across all 3 pages
    # Assert items are in descending executed_at order
```
