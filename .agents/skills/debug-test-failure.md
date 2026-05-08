# Skill: Debug Test Failure

You are working on the FX Engine — a FastAPI + PostgreSQL foreign exchange API.

## Import paths (current structure)

```python
from app.exceptions import FXError, QuoteNotFoundError, ...   # NOT app.core.exceptions
from app.providers.rates import rate_provider, _FALLBACK_MID  # NOT app.services.rates
from app.engine import fx                                      # NOT app.core.engine
from app.engine.fx import generate_quote, execute_quote
from app.services.database import get_pool, set_type_codecs
from app.models.customer import CustomerCreate, CustomerResponse
from app.models.quote import QuoteRequest, QuoteResponse
```

When updating tests, check for old-style imports (`app.core.*`, `app.api.routes.*`, `app.services.rates`) and update them to the new paths.

## Common failure patterns

### `ModuleNotFoundError: No module named 'app.core'`
Old import style. The `core/` directory was removed.
**Fix:** Update to `from app.exceptions import ...`, `from app.engine import fx`, etc.

### `ModuleNotFoundError: No module named 'app.services.rates'`
`rates.py` moved from `services/` to `providers/`.
**Fix:** `from app.providers.rates import rate_provider, _FALLBACK_MID, _compute_all_mids`

### `RuntimeError: Future attached to a different loop`
The asyncpg pool was created on a different event loop than the one running the test.
**Fix:** `asyncio_default_fixture_loop_scope = "function"` in `pyproject.toml`. The `clean_db` fixture closes and recreates the pool before every test.

### `InterfaceError: cannot perform operation: another operation is in progress`
Two coroutines sharing a single asyncpg connection concurrently.
**Fix:** For concurrent tests, use per-task mini-pools (`asyncpg.create_pool(min_size=1, max_size=1)`) — see `test_concurrency.py`.

### `KeyError: 'error_code'`
A router is catching `FXError` and re-raising as `HTTPException`, which changes the response to `{"detail": "..."}`.
**Fix:** Remove the try/except in the router — let `FXError` propagate to the app-level handler in `main.py`.

### Hypothesis falsifying example with `to_amount = 0.00`
Generated amount is too small for low-rate pairs (e.g. 0.01 NGN → 0.00 USD after 2dp rounding).
**Fix:** `min_value="10.00"` in the `decimals()` strategy.

### Staleness test not raising
Test sets `_fetched_at` to N seconds ago but N < `settings.rate_stale_seconds`.
**Fix:** Use `settings.rate_stale_seconds + 60` seconds, not a hardcoded value.

### Alembic migration fails mid-run
Most likely: referencing a column before it exists (e.g. creating an index on `country` before `ALTER TABLE ADD COLUMN country`).
**Fix:** Move index creation to the same migration that adds the column, or a later migration. Always run `alembic upgrade head` locally before committing.

## Debugging steps
1. Read the full traceback — the innermost frame is usually where the real issue is.
2. Check if the import path uses old-style (`app.core.*`, `app.api.routes.*`, `app.services.rates`).
3. Run just the failing test with `-v --tb=long` for full output.
4. Add a `print(resp.json())` before the failing assertion to see the actual response shape.
5. For Alembic failures: run `alembic history` and `alembic current` to check migration state.
