# Skill: Add API Endpoint

You are working on the FX Engine — a FastAPI + PostgreSQL foreign exchange API.

## App structure

```
app/
├── models/       Pydantic schemas — one file per domain (customer, quote, transaction, shared)
├── routes/       HTTP transport — thin, delegates to engine/services
├── engine/fx.py  FX business logic
├── providers/    External data (rates.py — RateProvider)
└── services/     Infrastructure (database, cache, events, metrics)
```

## Where new code goes

**New Pydantic model:** add to the relevant `models/` file, or create `models/your_domain.py`.

**New endpoint:** add to the relevant `routes/` file. Import schemas from `app.models.*`, database from `app.services.database`, engine from `app.engine.fx` or `app.engine`.

**New router:** create `app/routes/your_resource.py`, then add `app.include_router(your_router.router)` in `app/main.py`.

## Router conventions

```python
# app/routes/your_resource.py
from app.models.your_domain import YourRequest, YourResponse
from app.services.database import get_connection, get_pool

router = APIRouter(prefix="/your-resource", tags=["your-resource"])
log = structlog.get_logger(__name__)
```

## Database access

- **Read-only routes:** `conn: asyncpg.Connection = Depends(get_connection)`
- **Transactional routes:** call `pool = await get_pool()` then manage your own `async with pool.acquire() as conn: async with conn.transaction():`
- Always wrap numeric columns: `Decimal(str(row["amount"]))`

## Error handling

- Raise subclasses of `FXError` from `app.exceptions` — the app-level handler in `main.py` converts them to `{error, error_code, request_id}` format automatically.
- Do NOT catch `FXError` in routes and re-raise as `HTTPException` — that strips the `error_code` field from the response.
- For simple HTTP errors (duplicate email, not found on a non-domain check), `HTTPException` directly is fine.

## Logging

- `log = structlog.get_logger(__name__)` at module level.
- Log business events with relevant IDs as keyword args. Do not log request/response bodies — the middleware handles that.

## Tests

- Add tests in `tests/test_your_resource.py`.
- Use the `client` fixture from `conftest.py` for HTTP-layer tests.
- For concurrency/atomicity tests, call engine functions directly with a mini-pool (see `test_concurrency.py` pattern).
