# Skill: Add API Endpoint

You are working on the FX Engine — a FastAPI + PostgreSQL foreign exchange API.

## Project conventions to follow

### Router pattern
- Create or add to a file in `fx_engine/app/routers/`
- Register the router in `fx_engine/app/main.py` with `app.include_router(...)`
- Use Pydantic v2 models from `fx_engine/app/schemas.py` for request/response

### Database access
- **Read-only routes**: use `conn: asyncpg.Connection = Depends(get_connection)` from `app.database`
- **Write routes that need transactions**: accept `pool: asyncpg.Pool` and manage your own `async with pool.acquire() as conn: async with conn.transaction():`
- Always wrap `NUMERIC` column values: `Decimal(str(row["amount"]))`

### Error handling
- Raise subclasses of `FXError` from `app.exceptions` — they propagate to the app-level handler automatically
- Do NOT catch `FXError` and re-raise as `HTTPException` (strips the `error_code` field)
- For simple 404/409 from routers directly, `HTTPException` is fine

### Logging
- Use `log = structlog.get_logger(__name__)` at module level
- Log business events (created, updated) with relevant IDs as keyword args
- Do not log request/response bodies — the middleware handles that

### Tests
- Add tests in `fx_engine/tests/test_<resource>.py`
- Use the `client` fixture from `conftest.py`
- Business logic tests call engine functions directly with a pool from `get_pool()`
