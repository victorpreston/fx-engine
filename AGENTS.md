# Agent Instructions — FX Engine

This file records the instructions and constraints I gave to Claude Code when building this project, and my account of how the collaboration went.

---

## Role and Goal

Build a production-quality FX engine as described in `ASSIGNMENT.md`. Operate as a senior backend engineer: make opinionated decisions, explain trade-offs, prefer correctness over cleverness.

I treated AI tooling as a force-multiplier for structural scaffolding while owning every decision that touches financial correctness, concurrency safety, or system invariants.

---

## Stack Decisions (fixed, do not change)

- **Framework:** FastAPI (async) — not Flask. Async is idiomatic for FastAPI and makes concurrency tests with `asyncio.gather` clean. Flask's WSGI model would require a thread pool and `concurrent.futures`, adding noise to the concurrency proof.
- **Database:** PostgreSQL 16 via `asyncpg`. No ORM — direct SQL for full control over locking semantics. SQLite explicitly rejected because it cannot express `SELECT ... FOR UPDATE`.
- **Validation:** Pydantic v2.
- **Logging:** structlog with JSON output. No `print()` statements. No bare `logging.basicConfig`. Structured JSON is machine-parseable and maps directly to the correlation-ID requirement.
- **Metrics:** `prometheus_client` — the assignment requires `/metrics`; Prometheus format is the production standard.
- **Property tests:** Hypothesis — explicitly required by the assignment.
- **Test client:** `httpx.AsyncClient` with `ASGITransport` for HTTP-layer tests; direct engine function calls with per-task mini-pools for concurrency tests.
- **Middleware:** Raw ASGI class, not `BaseHTTPMiddleware`. Starlette's `BaseHTTPMiddleware` spawns anyio task groups that bind futures to a different event loop than asyncpg's pool — I discovered this under concurrent load and replaced it with a pure ASGI `ObservabilityMiddleware` that wraps `send()` directly.

---

## Constraints

1. **Never use `float` for financial arithmetic.** All amounts and rates must be `Decimal`. Converting to float and back (e.g., `float(amount) * float(rate)`) is a hard bug — and the exact pattern planted at `planted_bugs/fx.py:60`.
2. **Concurrency safety must be at the database level.** A `threading.Lock()` is not acceptable — it breaks under multi-worker deployments. Use `SELECT ... FOR UPDATE` inside a transaction.
3. **The rate must be locked at quote generation time.** The execution path must use `quote.rate` and `quote.to_amount` from the stored row. Never re-fetch the live rate at execution time — same bug as `planted_bugs/fx.py:126`.
4. **Idempotency check inside the transaction.** The idempotency key lookup must run inside the same database transaction as the `SELECT FOR UPDATE` on the quote, otherwise two concurrent retries with the same key can both pass the check (TOCTOU race).
5. **Deadlock prevention.** Always lock balance rows in alphabetical currency order. `sorted([from_ccy, to_ccy])` before `FOR UPDATE` is load-bearing.
6. **No secrets in code.** Use environment variables and `.env.example`. `.env` is gitignored.
7. **Tests must use real PostgreSQL.** No mocking of the database layer in concurrency or atomicity tests. Property tests may use pure functions.
8. **Commit messages follow Conventional Commits.** `feat:`, `fix:`, `test:`, `docs:`, `ci:`, `style:`, `chore:`. The git history is a graded deliverable.
9. **One feature per branch.** Branch from `dev`, merge back to `dev` with `--no-ff`. Branch history must show incremental, reviewable progress.
10. **`asyncpg` numeric values always wrapped.** `Decimal(str(row["field"]))` on every numeric column read. Without `set_type_codec("numeric", ...)`, asyncpg's handling varies between versions.

---

## What I Verified Myself

- **Spread direction:** the customer always gets `mid × (1 − spread)`, never better than mid. Verified manually that `buy < mid < sell` holds for every pair in `test_snapshot_sell_gt_mid_gt_buy`.
- **Deadlock ordering:** verified that PostgreSQL acquires row locks in the order rows are returned, so `ORDER BY currency FOR UPDATE` guarantees a stable lock order. Confirmed via PostgreSQL documentation and `test_different_quotes_execute_concurrently_no_deadlock`.
- **Idempotency TOCTOU fix:** confirmed the idempotency `fetchrow` runs before the quote `FOR UPDATE`, inside `conn.transaction()`. Read the function top-to-bottom and compared against constraint #4 before committing.
- **Cross-pair mid derivation:** verified `EUR/KES = USD/KES ÷ USD/EUR` against the seed values manually. All 12 pairs computed correctly from three USD-base rates.
- **asyncpg numeric codec round-trip:** inserted a balance, read it back, confirmed `Decimal(str(row["amount"]))` preserves precision through a full insert→select cycle.
- **Hypothesis strategy bounds:** `0.01 NGN × 0.000677 USD/NGN` rounds to `0.00 USD` — the falsifying example surfaced this. Raised `min_value` to `"10.00"` after manually confirming that 10 units of any supported currency always produces a positive `to_amount` after 2dp rounding.
- **Staleness threshold in tests:** hardcoded `700` seconds was below `RATE_STALE_SECONDS=3600` in CI — the staleness test never raised. Fixed to `settings.rate_stale_seconds + 60` after catching the false green locally before pushing.
- **GitHub Actions PostgreSQL health-check:** confirmed `pg_isready` correctly blocks the test job until the service is ready by reading CI logs on the first failing run.

---

## What I Delegated to the AI and Then Reviewed

- Initial boilerplate for FastAPI lifespan, middleware, and router structure — reviewed for correctness of startup/shutdown ordering and dependency injection patterns.
- Pydantic v2 schema models — reviewed that validators enforce supported currencies and positive amounts at the API boundary.
- Prometheus metric registration — reviewed that counter names match between registration and use, and that the gauge is set correctly in `/healthz`.
- Hypothesis `@given` strategies — reviewed that `allow_nan=False, allow_infinity=False` were set, and that `min_value` was appropriate for every currency pair.
- Docker Compose service definitions and health-check syntax — reviewed that `pg_isready` parameters match the service credentials.
- GitHub Actions workflow structure (service containers, job matrix, gate job) — reviewed that the `all-checks-pass` job correctly fails the PR if any dependency fails.
- SQL schema (`CREATE TABLE`, indexes, constraints) — reviewed that `CHECK (amount >= 0)` is present on balances and that the partial unique index on `idempotency_key` is correct.
- Initial `REVIEW.md` bug list structure — every finding independently verified against `planted_bugs/` source before including.
- `.claude/commands/` slash commands and `.agents/skills/` agent prompts.

---

## What I Rejected from the AI

These are drafts the AI produced that I rejected before they were committed:

| Draft | Reason |
|---|---|
| `float(amount) * float(rate)` in `generate_quote` | Same bug as `planted_bugs/fx.py:60` |
| `threading.Lock()` for execute concurrency | Invisible across OS processes; same class as `planted_bugs/fx.py:21` |
| Rate re-fetched at execute time | Violates the quote contract; same bug as `planted_bugs/fx.py:126` |
| Idempotency check outside the transaction | TOCTOU race; same class as `planted_bugs/fx.py:102` |
| `BaseHTTPMiddleware` for request tracing | Creates futures on the wrong event loop under concurrent asyncio load |
| `structlog.stdlib.add_logger_name` in processor chain | `PrintLogger` has no `.name` attribute — crashes every request |
| `asyncio_default_fixture_loop_scope = "session"` globally | Fixtures run on the session loop, test functions run on function loops — asyncpg pool mismatch |
| Shared pool for N concurrent test tasks | All `pool.acquire()` calls hit the same internal event-loop conflict |

---

## One Thing the AI Got Wrong

The first draft of `execute_quote` placed the idempotency key lookup **outside** the `conn.transaction()` block. This is the exact TOCTOU race described in the assignment: two concurrent retries both find no cached entry, both proceed to execute, and one receives a 500 from the UNIQUE constraint — the opposite of what idempotency guarantees.

I caught it by reading the function structure before the transaction block started, comparing against constraint #4, and moving the lookup to be the first statement inside `async with conn.transaction()`.

This class of bug — a check-then-act race where the check and the act are in different transaction scopes — is also the root of bugs 1 and 5 in `planted_bugs/`. LLMs reproduce common patterns; common patterns in financial code contain exactly these races.
