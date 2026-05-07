# Design Decisions

## Architecture and Trade-offs

### FastAPI over Flask
**Decision (mine):** FastAPI with async/await throughout.  
**Why:** The concurrency requirement — fire N parallel requests and assert exactly one succeeds — is naturally expressed with `asyncio.gather`. Flask's WSGI threading model would require a separate thread pool and `concurrent.futures`, which adds noise. FastAPI also gives automatic OpenAPI docs, Pydantic validation, and a cleaner dependency-injection pattern for the DB connection.  
**Trade-off:** asyncpg's API is less beginner-friendly than psycopg2. The `set_type_codec("numeric", ...)` configuration to handle PostgreSQL `NUMERIC` columns as strings is non-obvious and easy to get wrong. I verified this explicitly.

### PostgreSQL over SQLite
**Decision (mine):** PostgreSQL, mandatory.  
**Why:** `SELECT ... FOR UPDATE` is the only correct way to implement concurrency safety in a multi-process deployment. SQLite doesn't support row-level locking; its page-level locks would serialize all concurrent writes but still allow the check-then-act race if implemented naively (as the `planted_bugs/` code demonstrates). Any API server running more than one worker process — which is every production deployment — needs database-level locking.  
**Trade-off:** Requires Docker Compose for local setup vs. zero-config SQLite. Acceptable given the assignment's explicit note that "pure in-memory is not" acceptable.

### Direct SQL over ORM
**Decision (mine):** Raw `asyncpg` with hand-written SQL.  
**Why:** Financial systems benefit from precise control over locking syntax. An ORM generating `SELECT ... FOR UPDATE` requires knowing its exact dialect and version behavior. Writing `SELECT * FROM quotes WHERE id = $1 FOR UPDATE` is unambiguous. Direct SQL also makes the execution path easier to audit — every lock, every write, every constraint is visible in one place.  
**Trade-off:** More boilerplate for simple CRUD (customer creation, balance credit). Acceptable given the engine's core complexity is in the execute path, not CRUD.

### Rate Computation Strategy
**Decision (mine):** Pre-compute all 12 A/B pairs at refresh time from three USD-base rates; no runtime routing.  
**Why:** The `planted_bugs/` code's routing (try direct → try inverse → try cross) has a subtle bug in the cross-pair case where it uses the wrong rate direction. Pre-computing eliminates the routing logic entirely — every pair is a direct lookup. The derivation formula is simple and verifiable.  
**Trade-off:** Rates are computed in memory, not persisted per-refresh. A rate_snapshots table exists for auditability but is not used in the hot path.

### Idempotency Inside the Transaction
**Decision (mine):** The idempotency key lookup is the first statement inside `async with conn.transaction()`, before the `FOR UPDATE` lock.  
**Why:** If the lookup is outside the transaction, two concurrent requests with the same key can both find no cached entry, both proceed to execute, and one fails on the UNIQUE constraint. That gives the client an unexpected 500 on a retry — the opposite of what idempotency is supposed to guarantee.  
**Trade-off:** None. This is strictly correct. The AI's first draft put the lookup outside the transaction; I moved it in.

### Spread Applied as `mid × (1 − spread)` Only
**Decision (mine):** The customer always gets a rate below mid. The "buy" and "sell" rates exposed in `/rates` are for display; the `effective_rate` function is what drives all calculations.  
**Why:** Simpler and less error-prone than maintaining separate buy/sell rate tables and choosing the correct side per conversion direction. The economics are identical.  
**Trade-off:** The `/rates` snapshot shows buy/sell/mid for transparency, but the bid-ask framing is cosmetic.

---

## What I Delegated vs. Owned

| Decision | Who |
|----------|-----|
| FastAPI over Flask | Me |
| PostgreSQL + asyncpg | Me |
| Idempotency inside transaction | Me (caught AI's initial mistake) |
| Deadlock ordering (alphabetical currency) | Me |
| Rate pre-computation strategy | Me |
| Docker Compose service configuration | AI, reviewed |
| GitHub Actions job matrix | AI, reviewed |
| Hypothesis strategy parameters | AI, reviewed (verified `allow_nan=False`) |
| structlog processor chain | AI, reviewed |

---

## What I Rejected from the AI

1. **Float arithmetic for rate calculation.** The first draft of `generate_quote` used `float(amount) * float(rate)`. Rejected immediately — this is the same bug planted in `planted_bugs/fx.py:60`.  
2. **`threading.Lock()` for concurrency.** Rejected in favor of `SELECT FOR UPDATE`. A module-level lock is invisible to PostgreSQL and fails across workers.  
3. **Re-fetching the live rate at execution time.** The initial `execute_quote` draft called `_effective_rate()` again. Rejected — the locked-in rate is the entire value proposition of a quote.
4. **Idempotency lookup outside the transaction.** Moved inside.

---

## What I Did Not Trust Without Verifying

- **asyncpg numeric type handling** — tested explicitly that `Decimal(str(row["amount"]))` round-trips correctly through the `set_type_codec` configuration.  
- **`ORDER BY currency FOR UPDATE` deadlock prevention** — verified PostgreSQL acquires locks in the order rows are returned, so `ORDER BY` guarantees a stable lock order.  
- **Hypothesis generating edge-case Decimals** — confirmed `allow_nan=False, allow_infinity=False` prevent false-positive failures from non-numeric inputs.  
- **GitHub Actions `services` PostgreSQL health-check** — tested that the `pg_isready` health check in the workflow correctly gates the test job start.

---

## What I'd Do with Another Day

1. **Rate persistence** — write each refresh's rates to `rate_snapshots` and allow quote generation to reference the snapshot ID, enabling full rate audit trails.
2. **Webhook notifications** — `POST` to a customer-configured URL when a quote is executed.
3. **Quote streaming** — SSE endpoint streaming live rate updates so client UIs don't need to poll.
4. **Per-customer spread tiers** — premium customers get tighter spreads.
5. **Kubernetes manifests** — HPA, PodDisruptionBudget, readiness/liveness probes using `/healthz`.
6. **OpenTelemetry traces** — span the full quote → execute flow with the `trace_id` carried through.
