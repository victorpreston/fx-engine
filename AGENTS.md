# Agent Instructions — FX Engine

This file has two purposes: (1) the instructions I gave Claude Code at each stage, and (2) a transparent account of how the collaboration went — what the agent did well, what it got wrong, and where I overrode it. The grader can treat this as the process audit for the AI collaboration component.

---

## Role and Goal

Build a production-quality FX engine as described in `ASSIGNMENT.md`. Operate as a senior backend engineer: make opinionated decisions, explain trade-offs, prefer correctness over cleverness.

I treated AI tooling as a force-multiplier for structural scaffolding while owning every decision that touches financial correctness, concurrency safety, database schema, or system architecture. The rule I enforced throughout: **generated code was not trusted by default**. If it touched balances, rates, concurrency, or transactions, it needed a test to prove the property before I accepted it.

---

## Operating Principles (given to the agent)

1. **The spec is the contract.** Do not invent features the spec does not require. Do not skip requirements the spec marks as required.
2. **Surface uncertainty early.** If you're unsure whether the spec covers a case, stop and ask before writing code.
3. **Write the test first** when the behaviour is specified. Don't write tests after the fact to ratify whatever the code happens to do.
4. **Small, reviewable diffs.** Each step should compile and pass its own tests before moving on.
5. **You are not the decision-maker.** Flag trade-offs explicitly. Don't pick silently.

---

## Stack Decisions (fixed, do not change)

- **Framework:** FastAPI (async) — not Flask. Async is idiomatic for FastAPI and makes concurrency tests with `asyncio.gather` clean. Flask's WSGI model would require a thread pool and `concurrent.futures`, adding noise to the concurrency proof.
- **Database:** PostgreSQL 16 via `asyncpg`. No ORM — direct SQL for full control over locking semantics. SQLite explicitly rejected because it cannot express `SELECT ... FOR UPDATE`.
- **Migrations:** Alembic with raw SQL — the Python equivalent of KnexJS migrations. Versioned migrations: initial schema, compound indexes, customer enrichment, ledger + idempotency. `alembic upgrade head` runs at deploy time (in Dockerfile), not at runtime in the FastAPI lifespan.
- **Validation:** Pydantic v2. Schemas split by domain — one file per resource type.
- **Logging:** structlog with JSON output. No `print()` statements. No bare `logging.basicConfig`. Structured JSON is machine-parseable and maps directly to the correlation-ID requirement.
- **Metrics:** `prometheus_client` — the assignment requires `/metrics`; Prometheus format is the production standard. All counters are actually incremented (quotes_created/executed/expired, rate_fetch_success/failure, quote_errors by label, request latency histogram).
- **Event bus:** RabbitMQ via `aio-pika`. Durable topic exchange `fx.events` with routing keys `quote.created`, `quote.executed`, `quote.expired`. Published after transaction commit — never inside it.
- **Shared cache:** Redis for rate distribution across workers. On refresh, rates are written to Redis so other workers benefit without hitting the live API.
- **Property tests:** Hypothesis — explicitly required by the assignment.
- **Test client:** `httpx.AsyncClient` with `ASGITransport` for HTTP-layer tests; direct engine function calls with per-task mini-pools for concurrency tests.
- **Middleware:** Raw ASGI class, not `BaseHTTPMiddleware`. Starlette's `BaseHTTPMiddleware` spawns anyio task groups that bind futures to a different event loop than asyncpg's pool — discovered under concurrent load and replaced with a pure ASGI `ObservabilityMiddleware` that wraps `send()` directly.

---

## Constraints

1. **Never use `float` for financial arithmetic.** All amounts and rates must be `Decimal`. Converting to float and back is a hard bug — and the exact pattern planted in `planted_bugs/fx.py:60`.
2. **Concurrency safety must be at the database level.** A `threading.Lock()` is not acceptable — it breaks under multi-worker deployments. Use `SELECT ... FOR UPDATE` inside a transaction.
3. **The rate must be locked at quote generation time.** The execution path must use `quote.rate` and `quote.to_amount` from the stored row. Never re-fetch the live rate at execution time — same bug as `planted_bugs/fx.py:126`.
4. **Idempotency check inside the transaction.** The idempotency key lookup must run inside the same database transaction as the `SELECT FOR UPDATE` on the quote — otherwise two concurrent retries with the same key can both pass the check (TOCTOU race).
5. **`Idempotency-Key` is required on execute.** Missing key → HTTP 400. The key is bound to a SHA-256 hash of the request payload. Same key + different hash → HTTP 409.
6. **Deadlock prevention.** Always lock balance rows in alphabetical currency order. `sorted([from_ccy, to_ccy])` before `FOR UPDATE` is load-bearing.
7. **Ledger entries written in the same transaction as balance updates.** `balances` is a materialized cache; `ledger_entries` is the source of truth.
8. **No secrets in code.** Use environment variables and `.env.example`. `.env` is gitignored.
9. **Tests must use real PostgreSQL.** No mocking of the database layer in concurrency or atomicity tests. Property tests may use pure functions.
10. **Commit messages follow Conventional Commits.** `feat:`, `fix:`, `test:`, `docs:`, `ci:`, `style:`, `refactor:`, `chore:`. The git history is a graded deliverable.
11. **`asyncpg` numeric values always wrapped.** `Decimal(str(row["field"]))` on every numeric column read.
12. **Alembic runs at deploy time, not runtime.** Never call `alembic upgrade head` inside the FastAPI lifespan. It runs in the Dockerfile CMD before uvicorn starts.
13. **Events published after transaction commit.** Never publish to RabbitMQ inside a database transaction. The DB operation has already committed; the event must not cause a rollback.
14. **Migration ordering must be verified locally.** Run `alembic upgrade head` locally against the test database before committing any new migration.

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
- **psycopg2 URL format for Alembic:** verified the `postgresql+asyncpg://` → `postgresql://` prefix substitution in the Alembic env file handles both URL forms correctly.
- **Prometheus counters actually increment:** verified via `GET /metrics` after executing a quote — `fx_quotes_created_total` and `fx_quotes_executed_total` both showed `1.0`. Previous versions had the counters declared but never called `.inc()`.
- **Composite index column order:** verified `(customer_id, status, created_at DESC)` supports the primary dashboard query pattern through PostgreSQL's index scan planner.
- **Ledger invariant:** after every execute and credit, queried `SUM(credits) − SUM(debits)` from `ledger_entries` and compared against the cached balance. Values must match exactly.

---

## What I Delegated (and What I Personally Verified Before Accepting)

Scaffolding and boilerplate was delegated to the agent. I did not accept any of it without reading and verifying it myself:

- **FastAPI lifespan and router registration** — I checked startup/shutdown ordering and that the pool, Redis, and RabbitMQ connections close cleanly on shutdown.
- **Pydantic v2 schema models** — I studied each model to confirm validators reject unsupported currencies, non-positive amounts, and float inputs.
- **Prometheus metric registration** — I traced every counter declaration to its `.inc()` call site, and ran `GET /metrics` after a live execute to confirm values actually incremented.
- **Hypothesis `@given` strategies** — I read each strategy, confirmed `allow_nan=False, allow_infinity=False`, and worked out the correct `min_value` for each currency pair by hand.
- **Docker Compose service definitions** — I checked healthcheck parameters, volume mounts, and that the API container waits for PostgreSQL to be healthy before starting.
- **GitHub Actions workflow** — I read the gate job logic to confirm no test path could silently pass.
- **SQL schema DDL** — I studied every constraint: `CHECK (amount >= 0)` on balances, `CHECK (amount > 0)` on ledger_entries, direction and reference_type enums at the DB level.
- **Alembic `env.py`** — I traced the URL prefix substitution (`postgresql+asyncpg://` → `postgresql://`) and verified the psycopg2/asyncpg split was correct.
- **Grafana dashboard JSON** — I read each panel's PromQL query against the actual metric names in `metrics.py` to confirm they would return data.
- **RabbitMQ exchange topology** — I confirmed `TOPIC` exchange type and `durable=True` are correct for the task-queue pattern in use.
- **Redis rate cache TTL** — I worked through the math: `rate_stale_seconds + 120` ensures the cache outlives the staleness window so workers don't all hammer the upstream API simultaneously.

---

## What I Rejected from the AI

| Draft | Reason |
|---|---|
| `float(amount) * float(rate)` in `generate_quote` | Same bug as `planted_bugs/fx.py:60` |
| `threading.Lock()` for execute concurrency | Invisible across OS processes; same class as `planted_bugs/fx.py:21` |
| Rate re-fetched at execute time | Violates quote contract; same bug as `planted_bugs/fx.py:126` |
| Idempotency check outside transaction | TOCTOU race; same class as `planted_bugs/fx.py:102` |
| Idempotency-Key optional, no request hash | Client can reuse key with different payload and silently get wrong response |
| In-place balance updates, no ledger table | Unauditable; corrupted balance has no history to reconstruct from |
| Combined `/healthz` checking DB and rates | Stale rates would trigger liveness probe failure → unnecessary pod restart |
| `BaseHTTPMiddleware` for observability | Creates futures on wrong event loop under concurrent load |
| `add_logger_name` in structlog chain | `PrintLogger` has no `.name` — crashes every request |
| `asyncio_default_fixture_loop_scope = "session"` | Fixtures on session loop, tests on function loop — asyncpg pool mismatch |
| Shared pool for N concurrent test tasks | All `pool.acquire()` calls hit the same internal event-loop conflict |
| ORM-based Alembic autogenerate | No SQLAlchemy models to diff against; raw SQL in migrations is clearer and auditable |
| `run_migrations()` in the FastAPI lifespan | Migrations are a deploy-time concern, not runtime; moved to Dockerfile CMD |
| `idx_customers_country` in migration 002 | Column doesn't exist until migration 003 — caught by running locally before committing |
| Kafka for event publishing | Stream-processing tooling at the wrong scale; RabbitMQ is the correct fit for task-queue patterns |
| Redis Pub/Sub for events | No message persistence — consumers miss events if offline; RabbitMQ durable exchange guarantees delivery |

---

## Things the AI Got Wrong (and How I Caught Them)

**1. Idempotency lookup outside the transaction (TOCTOU race).**
The first draft of `execute_quote` placed the key lookup outside `conn.transaction()`. Two concurrent retries would both find no cached entry and race to execute — the second hits a UNIQUE constraint and returns a 500. I caught it by reading the function top-to-bottom before accepting the diff and comparing against constraint #4. Moved the lookup to be the first statement *inside* `async with conn.transaction():`.
This is structurally identical to Bug 5 in `planted_bugs/fx.py`. LLMs reproduce common code patterns; common patterns in financial code contain exactly these races.

**2. Idempotency key optional, no hash binding.**
The initial implementation made `Idempotency-Key` optional and stored only the key — no request hash. A client could reuse the same key with a different `quote_id` and silently receive the original response. I caught this by asking "what happens if the same key arrives with a different body?" — the answer exposed the gap. Changed to required header + SHA-256(endpoint + quote_id + customer_id) binding; mismatched hash → 409.

**3. `/healthz` combined liveness and readiness.**
The first implementation checked DB connectivity and rate freshness inside `/healthz`. I caught this when reading through the health check logic and realising that a stale-rates condition would cause a liveness probe to fail and trigger a pod restart — which cannot fix a provider outage and loses in-flight state. Split into `/healthz` (liveness only) and `/readyz` (DB + rates).

**4. In-place balance updates, no ledger.**
The initial schema had only a `balances` table. I identified the gap: in-place updates are unauditable — a corrupted balance has no history. Changed to a `ledger_entries` append-only table as source of truth, with `balances` as a materialized cache. The invariant `balance == SUM(credits) − SUM(debits)` is asserted in tests.

**5. `BaseHTTPMiddleware` for observability.**
The first middleware draft extended `BaseHTTPMiddleware`. This spawns anyio task groups that bind futures to a managed event loop — conflicting with asyncpg's pool under concurrent load. I replaced it with a raw ASGI class that wraps `send()` directly, no tasks, no cross-loop confusion.

**6. Prometheus counters declared but never wired.**
After the first full run I checked `GET /metrics` after executing a quote. `fx_quotes_executed_total` showed `0.0`. The agent declared all counters as module-level objects but never added the `.inc()` calls to the business logic. I traced each counter to its call site and verified all incremented correctly.

**7. Migration index before column existed.**
The agent placed `idx_customers_country` in migration 002. Running `alembic upgrade head` locally failed — the `country` column doesn't exist until migration 003. Moved the index to 003. This is why migration ordering is verified locally before every commit.

---

## Graded Requirements — Completion Checklist

| Requirement | How demonstrated |
|-------------|-----------------|
| Decimal precision throughout | Hypothesis property tests in `test_precision.py`; `float` is never used in any financial path |
| Concurrency safety (execute exactly once) | `test_concurrency.py` — N parallel executes, exactly 1 success via `SELECT FOR UPDATE` |
| Idempotency on execute | `test_idempotency.py` — required key (400 without), same-key replay, 409 on hash mismatch, concurrent retries write exactly one row |
| Atomic two-leg execution | `test_execute.py::test_atomicity_rollback_via_hook` — `_after_debit_hook` injects failure between debit and credit; asserts zero balance change, zero ledger rows, zero transaction rows |
| Rate-source failure handling | `test_rates.py` — staleness detection, refresh failure handling, Redis fallback |
| Observability | Correlation IDs in every log line; `/healthz` (liveness), `/readyz` (readiness + 503 on stale), `/metrics` (Prometheus) |
| Double-entry ledger | `ledger_entries` table; `test_execute.py::test_ledger_balance_invariant_after_execute` proves `balance == SUM(credits) − SUM(debits)` |
| Git history | Scoped Conventional Commits from scaffold through production hardening |
| SPEC.md | Written before any code; reflects the final implementation |
| DECISIONS.md | Trade-offs, AI delegation log, what was caught and overridden |
| REVIEW.md | 10 bugs in `planted_bugs/`, ranked by production impact |
