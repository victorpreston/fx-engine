# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Layout

```
fx_engine/          — the production API (FastAPI + asyncpg + PostgreSQL)
planted_bugs/       — AI-generated baseline for code review (Part 3 of assignment)
SPEC.md             — technical specification
AGENTS.md           — agent instructions used during development
DECISIONS.md        — architecture trade-offs and AI delegation log
REVIEW.md           — planted_bugs code review findings
```

All active development happens inside `fx_engine/`.

---

## Common Commands

Run from `fx_engine/`:

```bash
# Start test database
docker compose -f docker-compose.test.yml up -d

# Run full test suite
TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db \
  pytest -v

# Run a single test
TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db \
  pytest tests/test_execute.py::test_execute_debits_source_and_credits_destination -v

# Run a specific test file
TEST_DATABASE_URL=... pytest tests/test_concurrency.py -v

# Start the API locally (after setting DATABASE_URL in .env)
uvicorn app.main:app --reload --port 8000

# Full stack via Docker (API + PostgreSQL together)
docker compose up --build
```

The `.env` file must point to a running PostgreSQL. Copy `.env.example` then set `DATABASE_URL` to your running instance. The test DB runs on port 5433; the production compose DB runs on port 5432.

---

## Architecture

### Request lifecycle

```
HTTP request
  → ObservabilityMiddleware (pure ASGI — attaches X-Request-ID, times request)
  → FastAPI router (validates with Pydantic v2)
  → app/fx_engine.py  (core business logic — no HTTP types here)
  → asyncpg pool / PostgreSQL
```

`ObservabilityMiddleware` in `app/main.py` is a **raw ASGI class**, not `BaseHTTPMiddleware`. This is intentional — `BaseHTTPMiddleware` creates anyio task groups that conflict with asyncpg's event-loop binding under concurrent load.

### Core engine (`app/fx_engine.py`)

Two functions: `generate_quote` (takes a raw `asyncpg.Connection`) and `execute_quote` (takes the `asyncpg.Pool` and manages its own connection + transaction).

**Never re-fetch rates at execute time.** The rate and `to_amount` are locked into the `quotes` row at generation time and reused verbatim on execute.

### Concurrency safety

`execute_quote` uses `SELECT * FROM quotes WHERE id = $1 FOR UPDATE` inside a transaction. This is the only correct approach — application-level locks (`threading.Lock`) fail across multiple workers. Balance rows are locked in **alphabetical currency order** to prevent deadlocks.

### Rate provider (`app/rates.py`)

`RateProvider` is a module-level singleton. It fetches from `api.exchangerate-api.com` (USD base) and derives all 12 A/B pairs at refresh time. Rates are **pre-computed at refresh**, not routed at query time. Staleness is enforced: quotes raise 503 if `_fetched_at` is older than `RATE_STALE_SECONDS`.

### Database (`app/database.py`)

asyncpg pool with `set_type_codec("numeric", ...)` — this makes PostgreSQL `NUMERIC` columns arrive as `str` so they can be safely wrapped in `Decimal`. Always wrap asyncpg numeric results with `Decimal(str(row["field"]))`.

All schema is in `migrations/001_initial.sql` (idempotent `CREATE TABLE IF NOT EXISTS`). Migrations run automatically at startup via `run_migrations()`.

### Error handling

`FXError` subclasses (in `app/exceptions.py`) propagate naturally to the app-level `@app.exception_handler(FXError)` in `main.py`, which returns `{"error", "error_code", "request_id"}`. Do **not** catch `FXError` in routers and re-raise as `HTTPException` — that strips the `error_code` field.

---

## Test Architecture

### Event loop model

`asyncio_default_fixture_loop_scope = "function"` — each test gets its own event loop. The `clean_db` fixture closes and recreates the asyncpg pool before every test so the pool is always bound to the correct loop. `db_schema` is a **synchronous** session fixture that calls `asyncio.run()` for migrations.

### Concurrent tests

Tests in `test_concurrency.py` and the concurrent test in `test_idempotency.py` use **per-task mini-pools** (`min_size=1, max_size=1` per concurrent task) rather than a shared pool. This avoids asyncpg event-loop conflicts under concurrent `pool.acquire()` calls. The SQL-level concurrency guarantee (`SELECT FOR UPDATE`) is still real — transactions race on the same PostgreSQL row.

### Hypothesis tests (`test_precision.py`)

Use `min_value="10.00"` for amounts — lower values (e.g. 0.01 NGN) produce a `to_amount` of `0.00` after 2dp rounding, which is correct financial behaviour but breaks the "always positive" property. The CI profile runs with `derandomize=True` for reproducibility.

---

## Key Invariants

- **Float is never used** for any financial calculation. All amounts and rates are `Decimal` throughout.
- **`to_amount` is quantized once** at quote generation (`ROUND_HALF_UP`, 2dp). The execute path uses the stored value — it never recomputes.
- **Idempotency key lookup runs inside the transaction**, before the `FOR UPDATE` lock. Moving it outside creates a TOCTOU race.
- **Balance rows locked alphabetically** — `sorted([from_ccy, to_ccy])` before the `FOR UPDATE`. Changing this order risks deadlocks on concurrent opposite-direction conversions.
