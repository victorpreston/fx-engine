# Skill: Debug Test Failure

You are working on the FX Engine — a FastAPI + PostgreSQL foreign exchange API.

## Context
- Tests run with `asyncio_default_fixture_loop_scope = "function"` — each test gets its own event loop
- `clean_db` fixture closes and recreates the asyncpg pool before every test
- Concurrent tests use per-task mini-pools (not the shared pool) to avoid loop binding conflicts
- All asyncpg numeric results must be wrapped: `Decimal(str(row["field"]))`

## Common failure patterns

### `RuntimeError: Future attached to a different loop`
The asyncpg pool was created on a different event loop than the one running the test.
**Fix**: ensure the pool is created inside an async fixture that runs on the test's loop (not a session fixture). Check `conftest.py` — `clean_db` recreates the pool fresh.

### `InterfaceError: cannot perform operation: another operation is in progress`
Two coroutines are sharing a single asyncpg connection concurrently.
**Fix**: if writing a concurrent test, use per-task mini-pools (`asyncpg.create_pool(min_size=1, max_size=1)`) instead of sharing the global pool.

### `KeyError: 'error_code'`
A router is catching `FXError` and re-raising as `HTTPException`, which changes the response format to `{"detail": "..."}`.
**Fix**: remove the try/except in the router — let `FXError` propagate to the app-level handler in `main.py`.

### Hypothesis falsifying example with `to_amount = 0.00`
The generated amount is too small to produce a non-zero result after 2dp rounding for low-rate pairs (e.g. NGN/USD ≈ 0.00067).
**Fix**: set `min_value="10.00"` in the Hypothesis `decimals()` strategy.

### Staleness test not raising
Test sets `_fetched_at` to N seconds ago but N < `settings.rate_stale_seconds`.
**Fix**: use `settings.rate_stale_seconds + 60` seconds ago, not a hardcoded value.

## Debugging steps
1. Read the full traceback — the innermost frame is usually where the real issue is
2. Check what event loop scope is in play (`asyncio_default_fixture_loop_scope`)
3. Run just the failing test with `-v --tb=long` for full output
4. Add a `print(resp.json())` before the failing assertion to see the actual response shape
