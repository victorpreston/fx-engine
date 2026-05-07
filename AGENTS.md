# Agent Instructions — FX Engine

This file records the instructions and constraints given to Claude Code when building this project.

---

## Role and Goal

Build a production-quality FX engine as described in `ASSIGNMENT.md`. Operate as a senior backend engineer: make opinionated decisions, explain trade-offs, prefer correctness over cleverness.

---

## Stack Decisions (fixed, do not change)

- **Framework:** FastAPI (async) — not Flask. Async is idiomatic for FastAPI and makes concurrency tests with `asyncio.gather` clean.
- **Database:** PostgreSQL 16 via `asyncpg`. No ORM — direct SQL for full control over locking semantics. SQLite explicitly rejected because it cannot express `SELECT ... FOR UPDATE`.
- **Validation:** Pydantic v2.
- **Logging:** structlog with JSON output. No `print()` statements. No bare `logging.basicConfig`.
- **Metrics:** `prometheus_client`.
- **Property tests:** Hypothesis.
- **Test client:** `httpx.AsyncClient` with `ASGITransport`.

---

## Constraints

1. **Never use `float` for financial arithmetic.** All amounts and rates must be `Decimal`. Converting to float and back (e.g., `float(amount) * float(rate)`) is a hard bug.
2. **Concurrency safety must be at the database level.** A `threading.Lock()` is not acceptable — it breaks under multi-worker deployments. Use `SELECT ... FOR UPDATE` inside a transaction.
3. **The rate must be locked at quote generation time.** The execution path must use `quote.rate` and `quote.to_amount` from the stored row. Never re-fetch the live rate at execution time.
4. **Idempotency check inside the transaction.** The idempotency key lookup must run inside the same database transaction as the `SELECT FOR UPDATE` on the quote, otherwise two concurrent retries with the same key can both pass the check.
5. **Deadlock prevention.** Always lock balance rows in alphabetical currency order.
6. **No secrets in code.** Use environment variables and `.env.example`.
7. **Tests must use real PostgreSQL.** No mocking of the database layer in concurrency or atomicity tests. Property tests may use pure functions.
8. **Commit messages follow Conventional Commits.** `feat:`, `fix:`, `test:`, `docs:`, `ci:`, `refactor:`.
9. **One feature per branch.** Branch from `dev`, merge back to `dev`.

---

## What I Verified Myself

- The spread direction: customer always gets `mid × (1 − spread)`, never better than mid. Verified manually that `buy < mid < sell` for every pair in the snapshot test.
- The deadlock ordering: verified that the `ORDER BY currency` clause in the balance lock query matches the pre-insert sort used during the upsert.
- The idempotency TOCTOU fix: confirmed the idempotency fetchrow runs before the quote `FOR UPDATE` within the same transaction block.
- Cross-pair mid derivation: verified `EUR/KES = USD/KES ÷ USD/EUR` against the seed values manually.
- The `asyncpg` numeric codec: without `set_type_codec("numeric", ...)`, asyncpg returns `Decimal` objects natively in some versions and strings in others. Pinning to string and wrapping in `Decimal(str(...))` is the safe path.

---

## What I Delegated to the AI and Then Reviewed

- Initial boilerplate for FastAPI lifespan, middleware, and router structure.
- Prometheus metric registration — reviewed that names match between registration and use.
- Hypothesis `@given` strategies — reviewed that `allow_nan=False, allow_infinity=False` were set to prevent false test failures.
- Docker Compose service health-check syntax.
- GitHub Actions matrix and service container configuration.

---

## One Thing the AI Got Wrong

The first draft of `execute_quote` placed the idempotency key lookup **outside** the `conn.transaction()` block. This is the exact TOCTOU race described in the assignment. I caught it by reviewing the function structure before the transaction block started and moved the lookup to be the first statement inside `async with conn.transaction()`.
