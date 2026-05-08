# Agent Instructions — FX Engine

This file records the instructions and constraints I gave to Claude Code when building this project, and my account of how the collaboration went.

---

## Role and Goal

Build a production-quality FX engine as described in `ASSIGNMENT.md`. Operate as a senior backend engineer: make opinionated decisions, explain trade-offs, prefer correctness over cleverness.

I treated AI tooling as a force-multiplier for structural scaffolding while owning every decision that touches financial correctness, concurrency safety, database schema, or system architecture.

---

## Stack Decisions (fixed, do not change)

- **Framework:** FastAPI (async) — not Flask. Async is idiomatic for FastAPI and makes concurrency tests with `asyncio.gather` clean. Flask's WSGI model would require a thread pool and `concurrent.futures`, adding noise to the concurrency proof.
- **Database:** PostgreSQL 16 via `asyncpg`. No ORM — direct SQL for full control over locking semantics. SQLite explicitly rejected because it cannot express `SELECT ... FOR UPDATE`.
- **Migrations:** Alembic with raw SQL — the Python equivalent of KnexJS migrations. Three versioned migrations: initial schema, compound indexes, customer enrichment. `alembic upgrade head` runs at deploy time (in Dockerfile), not at runtime in the FastAPI lifespan.
- **Validation:** Pydantic v2. Schemas split by domain into `models/customer.py`, `models/quote.py`, `models/transaction.py`, `models/shared.py`.
- **Logging:** structlog with JSON output. No `print()` statements. No bare `logging.basicConfig`. Structured JSON is machine-parseable and maps directly to the correlation-ID requirement.
- **Metrics:** `prometheus_client` — the assignment requires `/metrics`; Prometheus format is the production standard. All counters are actually incremented (quotes_created/executed/expired, rate_fetch_success/failure, quote_errors by label, request latency histogram).
- **Event bus:** RabbitMQ via `aio-pika`. Durable topic exchange `fx.events` with routing keys `quote.created`, `quote.executed`, `quote.expired`. Published after transaction commit — never inside it.
- **Shared cache:** Redis for rate distribution across workers. On refresh, rates are written to Redis so other workers benefit without hitting the live API.
- **Property tests:** Hypothesis — explicitly required by the assignment.
- **Test client:** `httpx.AsyncClient` with `ASGITransport` for HTTP-layer tests; direct engine function calls with per-task mini-pools for concurrency tests.
- **Middleware:** Raw ASGI class, not `BaseHTTPMiddleware`. Starlette's `BaseHTTPMiddleware` spawns anyio task groups that bind futures to a different event loop than asyncpg's pool — discovered under concurrent load and replaced with a pure ASGI `ObservabilityMiddleware` that wraps `send()` directly.

---

## App Structure (fixed, do not change)

```
app/
├── main.py            app factory, lifespan, middleware, error handlers
├── config.py          pydantic-settings (reads DATABASE_URL, REDIS_URL, etc.)
├── exceptions.py      FXError hierarchy — flat file, no folder needed
├── models/            Pydantic schemas, one file per domain
│   ├── customer.py
│   ├── quote.py
│   ├── transaction.py
│   └── shared.py      SUPPORTED_CURRENCIES + health/rates/error schemas
├── routes/            HTTP transport — thin, delegates to engine/services
├── engine/            FX domain logic (generate_quote, execute_quote)
│   └── fx.py
├── providers/         External data sources (calls third-party APIs)
│   └── rates.py       RateProvider — exchangeratesapi.io
└── services/          Infrastructure we own
    ├── database.py    asyncpg pool
    ├── cache.py       Redis
    ├── events.py      RabbitMQ
    └── metrics.py     Prometheus counters + histogram
```

**The `providers/` vs `services/` distinction is deliberate:**  
`services/` = infrastructure we control (our PostgreSQL, Redis, RabbitMQ).  
`providers/` = external data we depend on (third-party exchange rate API).

---

## Database Folder Structure (fixed)

```
db/
└── versions/          Alembic migration files
alembic.ini            At project root — Alembic expects it here by default
```

**Migrations:**
- `001` — initial schema (customers, balances, quotes, transactions, rate_snapshots)
- `002` — compound indexes (quotes, transactions, customers — replaces naive single-column indexes)
- `003` — customer enrichment (phone, country, kyc_status) + quote audit fields (reference, mid_rate)

Always run `alembic upgrade head` before testing against a fresh database. Always run it locally before pushing.

---

## Constraints

1. **Never use `float` for financial arithmetic.** All amounts and rates must be `Decimal`. Converting to float and back is a hard bug — and the exact pattern planted in `planted_bugs/fx.py:60`.
2. **Concurrency safety must be at the database level.** A `threading.Lock()` is not acceptable — it breaks under multi-worker deployments. Use `SELECT ... FOR UPDATE` inside a transaction.
3. **The rate must be locked at quote generation time.** The execution path must use `quote.rate` and `quote.to_amount` from the stored row. Never re-fetch the live rate at execution time — same bug as `planted_bugs/fx.py:126`.
4. **Idempotency check inside the transaction.** The idempotency key lookup must run inside the same database transaction as the `SELECT FOR UPDATE` on the quote — otherwise two concurrent retries with the same key can both pass the check (TOCTOU race).
5. **Deadlock prevention.** Always lock balance rows in alphabetical currency order. `sorted([from_ccy, to_ccy])` before `FOR UPDATE` is load-bearing.
6. **No secrets in code.** Use environment variables and `.env.example`. `.env` is gitignored.
7. **Tests must use real PostgreSQL.** No mocking of the database layer in concurrency or atomicity tests. Property tests may use pure functions.
8. **Commit messages follow Conventional Commits.** `feat:`, `fix:`, `test:`, `docs:`, `ci:`, `style:`, `refactor:`, `chore:`. The git history is a graded deliverable.
9. **One feature per branch.** Branch from `dev`, merge back to `dev` with `--no-ff`.
10. **`asyncpg` numeric values always wrapped.** `Decimal(str(row["field"]))` on every numeric column read.
11. **Alembic runs at deploy time, not runtime.** Never call `alembic upgrade head` inside the FastAPI lifespan. It runs in the Dockerfile CMD before uvicorn starts.
12. **Events published after transaction commit.** Never publish to RabbitMQ inside a database transaction. The DB operation has already committed; the event must not cause a rollback.
13. **Migration ordering must be verified locally.** Run `alembic upgrade head` locally against the test database before committing any new migration. Index creation on a column that doesn't exist yet will fail at runtime, not at lint time.

---

## What I Verified Myself

- **Spread direction:** the customer always gets `mid × (1 − spread)`, never better than mid. Verified manually that `buy < mid < sell` holds for every pair in `test_snapshot_sell_gt_mid_gt_buy`.
- **Deadlock ordering:** verified that PostgreSQL acquires row locks in the order rows are returned, so `ORDER BY currency FOR UPDATE` guarantees a stable lock order.
- **Idempotency TOCTOU fix:** confirmed the idempotency `fetchrow` runs before the quote `FOR UPDATE`, inside `conn.transaction()`.
- **Cross-pair mid derivation:** verified `EUR/KES = USD/KES ÷ USD/EUR` against the seed values manually.
- **asyncpg numeric codec round-trip:** inserted a balance, read it back, confirmed `Decimal(str(row["amount"]))` preserves precision through a full insert→select cycle.
- **Hypothesis strategy bounds:** `0.01 NGN × 0.000677 USD/NGN` rounds to `0.00 USD` — the falsifying example surfaced this. Raised `min_value` to `"10.00"`.
- **Staleness threshold in tests:** hardcoded `700` seconds was below `RATE_STALE_SECONDS=3600` in CI. Fixed to `settings.rate_stale_seconds + 60`.
- **Alembic migration ordering:** ran `alembic upgrade head` locally before every push. Migration 002 originally referenced the `country` column before it was added in 003 — caught locally, fixed before committing.
- **psycopg2 URL format for Alembic:** verified the `postgresql+asyncpg://` → `postgresql://` prefix substitution in `db/env.py` handles both URL forms correctly.
- **Prometheus counters actually increment:** verified via `GET /metrics` after executing a quote — `fx_quotes_created_total` and `fx_quotes_executed_total` both showed `1.0`. Previous versions had the counters declared but never called `.inc()`.
- **Composite index column order:** verified `(customer_id, status, created_at DESC)` supports the primary dashboard query pattern through PostgreSQL's index scan planner.

---

## What I Delegated to the AI and Then Reviewed

- FastAPI lifespan scaffold and router registration — reviewed startup/shutdown ordering.
- Pydantic v2 schema models — reviewed validators enforce supported currencies and positive amounts.
- Prometheus metric registration — reviewed counter names match between declaration and increment calls, and that the histogram buckets are appropriate for sub-second API latency.
- Hypothesis `@given` strategies — reviewed `allow_nan=False, allow_infinity=False`, and that `min_value` was appropriate per pair.
- Docker Compose service definitions — reviewed healthcheck parameters, volume mounts, and service dependency ordering.
- GitHub Actions workflow structure — reviewed `all-checks-pass` gate job logic.
- SQL schema DDL — reviewed that `CHECK (amount >= 0)` is present on balances and that the partial unique index on `idempotency_key` is correct.
- Alembic `env.py` — reviewed URL prefix handling, `psycopg2` vs `asyncpg` driver separation, and `target_metadata = None` (no ORM autogenerate).
- Grafana dashboard JSON — reviewed PromQL queries for correctness against actual metric names.
- RabbitMQ exchange topology — reviewed that `TOPIC` exchange type and `durable=True` are correct for the use case.
- Redis rate cache TTL calculation — reviewed that `rate_stale_seconds + 120` buffer prevents cache expiry coinciding with the staleness window.
- `.claude/commands/` and `.agents/skills/` content — reviewed for accuracy against the actual codebase.

---

## What I Rejected from the AI

| Draft | Reason |
|---|---|
| `float(amount) * float(rate)` in `generate_quote` | Same bug as `planted_bugs/fx.py:60` |
| `threading.Lock()` for execute concurrency | Invisible across OS processes; same class as `planted_bugs/fx.py:21` |
| Rate re-fetched at execute time | Violates quote contract; same bug as `planted_bugs/fx.py:126` |
| Idempotency check outside transaction | TOCTOU race; same class as `planted_bugs/fx.py:102` |
| `BaseHTTPMiddleware` for observability | Creates futures on wrong event loop under concurrent load |
| `add_logger_name` in structlog chain | `PrintLogger` has no `.name` — crashes every request |
| `asyncio_default_fixture_loop_scope = "session"` | Fixtures on session loop, tests on function loop — asyncpg pool mismatch |
| Shared pool for N concurrent test tasks | All `pool.acquire()` calls hit the same internal event-loop conflict |
| ORM-based Alembic autogenerate | No SQLAlchemy models to diff against; raw SQL in migrations is clearer and auditable |
| `run_migrations()` in the FastAPI lifespan | Migrations are a deploy-time concern, not runtime; moved to Dockerfile CMD |
| `idx_customers_country` in migration 002 | Column doesn't exist until migration 003 — caught by running locally before committing |
| `grafana/` as a top-level folder | Names a tool, not a concept; renamed to `monitoring/` |
| All schemas in one `schemas.py` | 149-line file mixing all domains; split into `models/{customer,quote,transaction,shared}.py` |
| `services/rates.py` | RateProvider is not an infrastructure adapter we own — it's an external data source; moved to `providers/rates.py` |

---

## One Thing the AI Got Wrong

The first draft of `execute_quote` placed the idempotency key lookup **outside** the `conn.transaction()` block. This is the exact TOCTOU race described in the assignment: two concurrent retries both find no cached entry, both proceed to execute, and one receives a 500 from the UNIQUE constraint — the opposite of what idempotency guarantees.

I caught it by reading the function structure before the transaction block started, comparing against constraint #4, and moving the lookup to be the first statement inside `async with conn.transaction()`.

This class of bug — a check-then-act race where the check and the act are in different transaction scopes — is also the root of bugs 1 and 5 in `planted_bugs/`. LLMs reproduce common patterns; common patterns in financial code contain exactly these races.
