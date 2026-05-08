# AGENTS.md — AI Collaboration Record

This document records how Claude Code was used throughout this project: the instructions given, the constraints enforced, the decisions owned, the things the AI got wrong, and the things I verified before trusting.

---

## Role and Operating Mandate

The agent was instructed to operate as a **senior backend engineer making production-grade decisions**, not as a code generator producing boilerplate. Specific framing given:

- Prefer correctness and auditability over brevity
- Make opinionated technology choices and defend them
- Every non-trivial decision must have a documented trade-off
- Treat the assignment as a real production system, not a demo
- The code review section (`planted_bugs/`) is as important as the implementation

---

## Technology Constraints (non-negotiable)

These were specified before any code was written:

| Constraint | Reason given |
|---|---|
| FastAPI (async), not Flask | `asyncio.gather` concurrency tests are idiomatic; Flask's WSGI model adds noise |
| PostgreSQL, not SQLite | `SELECT ... FOR UPDATE` requires row-level locking; SQLite cannot express it |
| Raw `asyncpg`, no ORM | Locking semantics must be explicit; ORM-generated SQL is harder to audit in financial code |
| Python `Decimal` everywhere | `float` arithmetic is a hard financial bug; `float(amount) * float(rate)` is the exact pattern planted in `planted_bugs/` |
| `structlog` JSON output | Structured logs are machine-parseable; `logging.basicConfig` produces unstructured strings |
| `prometheus_client` for metrics | Assignment requires `/metrics`; Prometheus format is the production standard |
| `Hypothesis` for property tests | Assignment explicitly requires property-based testing |
| Real PostgreSQL in all concurrency/atomicity tests | Mocking the DB layer cannot prove row-level locking works |
| Conventional Commits for all messages | Reviewers will read the git history; it is a graded deliverable |
| One feature branch per concern | Branch history must show incremental, reviewable progress |

---

## Hard Constraints Enforced Throughout

1. **`float` is never used for financial arithmetic.** Any draft that used `float()` was rejected immediately. The planted bug at `planted_bugs/fx.py:60` (`float(amount) * float(rate)`) is the canonical example of why.

2. **Concurrency safety lives in the database.** Application-level locks (`threading.Lock`, `asyncio.Lock`) are invisible to other OS processes and workers. Every draft that proposed these was rejected in favour of `SELECT ... FOR UPDATE` inside an explicit PostgreSQL transaction.

3. **Rate locked at quote time, never re-fetched on execute.** The first `execute_quote` draft called `_effective_rate()` again at execution time. This is the planted bug at `planted_bugs/fx.py:126–132`. Rejected: the customer's agreement is with the quoted rate, not with whatever the market does in the next 60 seconds.

4. **Idempotency check runs inside the transaction.** The first draft placed the `SELECT FROM transactions WHERE idempotency_key = $1` lookup before `async with conn.transaction():`. Two concurrent retries with the same key could both find nothing, both execute, and one would receive a 500 from the UNIQUE constraint — the opposite of idempotency. Moved inside the transaction as the first statement, before the `FOR UPDATE` lock.

5. **Balance rows locked in alphabetical currency order.** Without a deterministic lock order, two concurrent transactions converting USD→EUR and EUR→USD simultaneously deadlock. The `sorted([from_ccy, to_ccy])` call before the `FOR UPDATE` is load-bearing.

6. **`asyncpg` numeric codec pinned to string.** Without `set_type_codec("numeric", encoder=str, decoder=str, ...)`, asyncpg's handling of PostgreSQL `NUMERIC` columns varies between versions. All numeric reads are wrapped: `Decimal(str(row["field"]))`.

7. **No secrets in source.** All configuration via environment variables. `.env` is gitignored; `.env.example` is committed.

---

## What Was Delegated to the AI

The following were delegated with explicit instructions to produce working drafts, which were then reviewed before acceptance:

- FastAPI lifespan scaffold (startup/shutdown hooks)
- Pydantic v2 schema models (request/response shapes)
- Docker Compose service definitions and health-check syntax
- GitHub Actions workflow structure (service containers, job dependencies)
- `structlog` processor chain configuration
- `prometheus_client` counter/gauge registrations
- Hypothesis `@given` strategy parameters
- SQL schema (CREATE TABLE statements) — reviewed for constraint correctness
- Initial `REVIEW.md` bug list structure — every finding independently verified against the source
- `.claude/` commands and `.agents/` skill definitions

---

## What Was Rejected from the AI

Each of the following was proposed in an initial draft and explicitly rejected:

| Draft | Why rejected |
|---|---|
| `float(amount) * float(rate)` in `generate_quote` | Same bug as `planted_bugs/fx.py:60`; introduces IEEE 754 error |
| `threading.Lock()` for execute concurrency | Invisible to other workers; gives false confidence |
| `_effective_rate()` called again in `execute_quote` | Violates quote contract; same bug as `planted_bugs/fx.py:126` |
| Idempotency check outside transaction | TOCTOU race; same class of bug as `planted_bugs/fx.py:102` |
| `BaseHTTPMiddleware` for observability | Spawns anyio task groups that create futures on the wrong event loop under concurrent load — causes `RuntimeError: Future attached to a different loop` |
| `structlog.stdlib.add_logger_name` in processor chain | Reads `.name` from the logger; `PrintLogger` has no `.name` — crashes every request |
| `asyncio_default_fixture_loop_scope = "session"` for test fixtures | Fixtures run on the session loop; test functions run on function loops; asyncpg pool binds to whichever loop created it — mismatch causes `InterfaceError` under concurrent tests |
| Shared pool for concurrent test tasks | All 20 `pool.acquire()` calls on the same pool under concurrent asyncio tasks triggers the same event-loop mismatch internally — replaced with per-task mini-pools (`min_size=1, max_size=1`) |
| `_FALLBACK_MID` keys as pair format (`"USD/EUR"`) | `_compute_all_mids()` expects currency-code keys (`"EUR"`); wrong keys cause `KeyError` at collection time |
| Catching `FXError` in routers and re-raising as `HTTPException` | Strips `error_code` from the response — tests checking `resp.json()["error_code"]` would `KeyError` |
| Hypothesis `min_value="0.01"` for all pairs | `0.01 NGN × 0.000677 USD/NGN = 0.0000067` rounds to `0.00` — falsifies the "always positive" property |
| Hardcoded `700` seconds for staleness tests | `RATE_STALE_SECONDS=3600` in CI; `700 < 3600` so the test never raised — replaced with `settings.rate_stale_seconds + 60` |

---

## Things the AI Got Wrong (caught during review)

These were bugs in generated code, caught before they were committed:

**1. Idempotency lookup outside the transaction** *(highest severity)*  
The first `execute_quote` draft opened the transaction after checking the idempotency key. This is an exact TOCTOU race: two concurrent retries both see no cached row, both execute, one gets a 500. Caught by reading the function top-to-bottom and comparing against the invariant stated in the constraints.

**2. `BaseHTTPMiddleware` event-loop conflict**  
The first middleware implementation used `@app.middleware("http")` (Starlette's `BaseHTTPMiddleware`). Under concurrent load in tests, anyio's task group created futures bound to its internal loop, while asyncpg's pool was bound to asyncio's loop — producing `RuntimeError: Future attached to a different loop`. Caught by running the concurrency test locally and reading the full traceback. Fixed by replacing with a raw ASGI class (`ObservabilityMiddleware`) that wraps `send()` directly.

**3. pytest-asyncio session-scoped event loop**  
Setting `asyncio_default_fixture_loop_scope = "session"` makes fixtures use the session loop, but test *functions* still run on per-test function loops. The asyncpg pool created in a fixture was on the session loop; the test body running on a function loop saw a mismatch and produced `InterfaceError: cannot perform operation: another operation is in progress`. Caught by reproducing locally and reading asyncpg's source to understand pool loop-binding. Fixed by reverting to `asyncio_default_fixture_loop_scope = "function"` and making `db_schema` a synchronous fixture using `asyncio.run()`.

**4. Rate re-fetched at execution time**  
`execute_quote` called `self._effective_rate(row["from_currency"], row["to_currency"])` instead of using `row["rate"]`. The customer's quote is a contract for a specific rate — re-fetching it at execution time breaks that contract and introduces financial exposure. Caught by reading the execute path and comparing against the spec invariant before the function was committed.

---

## What I Verified Before Trusting

- **Spread direction:** that `buy < mid < sell` holds for every pair. Verified by running `test_snapshot_sell_gt_mid_gt_buy` and reading `_compute_all_mids` output manually.
- **Deadlock ordering:** that PostgreSQL acquires row locks in the order rows are returned by a query with `ORDER BY`. Confirmed via PostgreSQL documentation and the `test_different_quotes_execute_concurrently_no_deadlock` test.
- **asyncpg numeric codec round-trip:** that `Decimal(str(row["amount"]))` survives a full insert→select cycle. Verified by adding a balance, reading it back, and checking the type and value.
- **Idempotency within the transaction:** that the lookup runs before `FOR UPDATE` and inside `conn.transaction()`. Verified by reading the function structure and checking PostgreSQL docs on transaction isolation.
- **GitHub Actions PostgreSQL health-check:** that `pg_isready` correctly blocks the test job until the service is ready. Verified by checking CI logs after the first failing run.
- **Hypothesis strategy bounds:** that `allow_nan=False, allow_infinity=False` and `min_value="10.00"` are necessary. `0.01 NGN` rounds to `0.00 USD` — verified the falsifying example locally before raising the bound.
- **`ruff check` and `ruff format`:** ran both locally before every push to confirm zero lint and format errors.

---

## CI/CD and Tooling Choices

- **GitHub Actions:** Two workflows — `pr-checks.yml` (lint + tests + Docker build on every PR to `dev`, `staging`, `fx-prod`) and `test.yml` (full suite on push to any main branch). A third `production.yml` runs exclusively on `fx-prod` pushes: tests → Docker build → end-to-end smoke test (customer → credit → quote → execute) → summary gate.
- **Branch model:** `feature/*` → `dev` → `staging` → `fx-prod`. Feature branches merged with `--no-ff` to preserve history. `fx-prod` promoted manually via GitHub PR to create an explicit production gate.
- **Ruff:** both `ruff check` (lint) and `ruff format --check` enforced in CI. All 11 lint errors and 19 formatting issues fixed before final submission.
- **Claude Code project config:** `.claude/settings.json` pre-approves common commands (pytest, docker compose, git) to reduce prompt friction. `.claude/commands/` provides project-specific slash commands. `.agents/skills/` contains reusable agent prompts for future contributors (add-currency-pair, add-endpoint, debug-test-failure, security-review, rate-source-swap).

---

## Process Notes

The assignment states: *"We want to see how you work with these tools, not how you'd write every line by hand."*

My approach: use the AI for structural scaffolding and boilerplate, but own every invariant that touches financial correctness, concurrency safety, or the test architecture. The AI is fast at producing working first drafts; the engineer's job is to read them critically against the spec and reject anything that trades correctness for convenience.

The four bugs the AI produced (idempotency outside transaction, middleware event-loop conflict, session loop mismatch, rate re-fetch) are all exactly the class of subtle, non-obvious mistakes that a code reviewer must catch — and they are structurally similar to the bugs planted in `planted_bugs/`. This is not coincidental: LLMs tend to reproduce common patterns, and common patterns in financial code often contain exactly these races and precision errors.
