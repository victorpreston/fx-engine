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

### Alembic for Migrations
**Decision (mine):** Alembic with raw SQL migration files — no ORM autogenerate.  
**Why:** The Python equivalent of KnexJS migrations. Alembic tracks applied versions in an `alembic_version` table, supports `upgrade head` and `downgrade -1`, and produces a full `alembic history` audit trail. The alternative — a hand-rolled runner executing a single `001_initial.sql` at startup — had no version tracking, no rollback, and silently ignored schema drift (`CREATE TABLE IF NOT EXISTS` succeeds even if the table is wrong).  
**Why not Alembic's autogenerate:** Autogenerate infers schema from SQLAlchemy ORM models. We use raw asyncpg, so there are no models to diff against. We write raw SQL in each revision's `upgrade()` function — the same SQL we'd write by hand, but versioned and tracked.  
**Why psycopg2 for Alembic, asyncpg for the runtime:** Alembic is a CLI tool that runs synchronously before the app starts. It doesn't need async. Adding asyncpg as a synchronous Alembic driver is complex and unnecessary. psycopg2 is only used by Alembic; the live API uses asyncpg exclusively.  
**Migration as deploy step:** `Dockerfile` runs `alembic upgrade head && uvicorn ...`. Migrations are a deploy-time concern, not a runtime concern — they do not run inside the FastAPI lifespan.


### Five Migrations — Rationale for Each

**`be56cd133310` — initial_schema**  
All tables in one migration: `customers`, `balances`, `quotes`, `transactions`, `rate_snapshots`. Indexes created here: `idx_quotes_customer` (single-column — later replaced), `idx_quotes_status_expires` (for expired-quote background cleanup), `idx_transactions_idempotency` (partial unique), `idx_transactions_customer`, `idx_rate_snapshots_pair_time`.

**`1076c0e1bb3f` — compound_indexes**  
Separated from the initial schema intentionally — indexes are a tuning decision, not a structural one. Rolling back index strategy should never touch table definitions. Changes in this revision:
- Drops `idx_quotes_customer` (single-column, subsumed by the composite — keeping it wastes write overhead on every quote insert)
- Creates `idx_quotes_customer_status_created ON quotes(customer_id, status, created_at DESC)` — matches the "show me this customer's pending quotes, newest first" dashboard query pattern exactly
- Creates `idx_transactions_customer_date ON transactions(customer_id, executed_at DESC)` — matches the account statement query pattern

**`aadaaac35570` — customer_enrichment_and_quote_audit_fields**  
Product decisions reflected as schema changes, kept separate from structural (first migration) and tuning (second) changes:
- `customers.phone` — primary contact channel for M-Pesa notifications; Kenya is phone-first
- `customers.country CHAR(2)` — ISO 3166-1 alpha-2; required for regulatory reporting and future per-country spread tiers
- `customers.kyc_status CHECK (pending/verified/rejected) DEFAULT pending` — legally required before FX execution in production; constraint lives in the DB from day one even before enforcement is added at the application layer
- `quotes.reference` — client-provided reconciliation reference (e.g. INV-2026-001); stored verbatim
- `quotes.mid_rate NUMERIC(20,8)` — market mid-rate at quote time; combined with the stored effective rate proves the exact spread applied, essential for dispute resolution
- `idx_customers_country` partial index (`WHERE country IS NOT NULL`) — placed here, not in the previous migration, because the column doesn't exist until this revision runs

**`c4f1a2b3d8e9` — ledger_and_idempotency**  
Added after a final cross-check exposed two gaps: money movement had no append-only audit trail, and idempotency was tied only to `transactions.idempotency_key`.
- `ledger_entries` — append-only source of truth for credits and executions; `balances` is the materialized cache
- `idempotency_keys` — dedicated table for `(endpoint, key, request_hash, response_payload, status_code, completed_at)`
- `idx_ledger_ref_direction` — prevents one execution from writing duplicate debit or duplicate credit rows
- `idx_idempotency_keys_created_at` — supports future retention cleanup

**`f2e3a4b5c6d7` — ledger_immutability_trigger**  
Adds a PostgreSQL `BEFORE UPDATE OR DELETE` trigger on `ledger_entries`. Corrections must be reversing entries, not edits to history. This was a final hardening pass: the table being called "append-only" in code comments was not enough until the database enforced it.

### Append-Only Ledger Over In-Place Balance Updates
**Decision (mine):** All money movements write paired rows to `ledger_entries` (debit + credit) in the same transaction as the balance `UPDATE`. `balances` is a materialized cache; `ledger_entries` is the source of truth.  
**Why:** In-place balance updates are unauditable — if a bug corrupts a balance, the history is gone. An append-only ledger means:
1. Balances can be independently reconciled from `SUM(credits) − SUM(debits)` at any point in time.
2. The audit trail is write-once and can be protected by a DB trigger that rejects `UPDATE` and `DELETE`.
3. Every money movement has a `reference_id` pointing to the triggering record (execution or credit adjustment).  
**Trade-off:** Two extra `INSERT`s per execution (one debit, one credit). At Umba's transaction volume this is negligible. The operational benefit — being able to answer "show me every cent that moved through this customer's account" from the ledger alone — is material.  
**Note:** `balances` is kept as a cache rather than derived on every read for performance. A reconciliation test asserts `balance == SUM(credits) − SUM(debits)` after every money-moving operation. A database trigger now rejects `UPDATE` and `DELETE` on `ledger_entries`; the DB, not only the application, protects the audit trail.

### Split `/healthz` and `/readyz`
**Decision (mine):** Two endpoints with different contracts, not one combined check.  
**Why:** A load balancer uses the liveness probe to decide whether to restart a container. If the liveness probe checks rate freshness, a stale-rates condition (provider down for 10 minutes) causes Kubernetes to restart the pod — which does not fix the problem and loses the in-flight state. A readiness probe checks whether the instance should receive traffic; failing readiness pulls it out of rotation without restarting it.  
**`/healthz`:** Always `{"status": "ok"}` as long as the process is alive. No DB, no rate check.  
**`/readyz`:** DB ping + rate freshness. Returns HTTP 503 when stale or unreachable so the load balancer routes away.  
**What I overrode:** The initial implementation combined both checks into `/healthz`. I caught this during the production-readiness review and split them.

### Idempotency Key Required, Bound to Request Hash
**Decision (mine):** `Idempotency-Key` is a required header on execute; missing key returns HTTP 400. The key is stored with a `SHA-256(method + path + body)` hash; replaying the same key with a different payload returns HTTP 409.  
**Why:** An optional key is not real idempotency — if the client omits it, each retry creates a new execution. Making it required means every client is forced to participate in the retry-safety contract.  
**Why bind to request hash:** A key reused with a different `quote_id` is almost certainly a client bug (they are constructing keys incorrectly) or an attack. Returning 409 rather than silently executing with the new payload is the correct safety-first behaviour.  
**What I caught, first pass:** The first implementation stored the key in the `transactions` table and looked it up by key alone — no hash check. A client could reuse a key with a completely different quote and receive the cached response for the original quote, silently executing neither or both. Moved to a dedicated `idempotency_keys` table with `(endpoint, key, request_hash, response_payload)`.
**What I caught, final pass:** The docs said `response_payload` was stored, but the code only set `completed_at` and `status_code`. Replay still returns the transaction-shaped response because that is the existing route contract, but the idempotency table now carries the response snapshot it claimed to carry. I fixed the completion step to persist a JSONB snapshot and added coverage around the replay path.
**In-flight retry behaviour:** I also caught that if an idempotency row existed with `completed_at IS NULL`, the code continued into execution instead of treating the key as in-flight. That is not safe under same-key concurrent retries. The current behavior returns `execution_in_progress` (409) and tells the client to retry shortly, rather than racing to execute twice.

### Rate Computation Strategy
**Decision (mine):** Pre-compute all 12 A/B pairs at refresh time from three USD-base rates; no runtime routing.  
**Why:** The `planted_bugs/` code's routing (try direct → try inverse → try cross) has a subtle bug in the cross-pair case where it uses the wrong rate direction. Pre-computing eliminates the routing logic entirely — every pair is a direct lookup. The derivation formula is simple and verifiable.  
**Trade-off:** The hot path still reads from the in-memory/Redis cache for speed, but each successful refresh now persists all 12 mid-rate rows to `rate_snapshots` for auditability. I did not add the fuller `rate_refreshes/current_rates/quote_legs` model; for this time-box, `quotes.mid_rate` plus persisted `rate_snapshots` gives enough rate provenance without a larger schema rewrite.

### Rate Snapshots on Refresh
**Decision (mine):** Persist one `rate_snapshots` row per currency pair after every successful provider refresh.  
**Why:** A table named `rate_snapshots` that never receives rows is worse than no table: it advertises auditability without delivering it. A final cross-check found that the provider only updated memory and Redis. I wired `_do_refresh()` to persist the computed 12-pair snapshot after the cache write and added a test that a successful refresh adds exactly 12 snapshot rows.
**Trade-off:** Snapshot persistence is best-effort after cache refresh. If the DB insert fails, the refresh remains available in memory/Redis and the failure is logged. A production version would move refresh audit into a single transaction with a `rate_refreshes` parent row, but that was outside the final catch-up scope.

### Error Envelope Over RFC 7807
**Decision (mine):** Keep a simplified error envelope: `{"error": "...", "error_code": "...", "request_id": "..."}` rather than adopting `application/problem+json`.  
**Why:** The final cross-check flagged that the implementation did not use RFC 7807. I chose not to do a late API-wide format migration because the current shape is already machine-readable, stable, and documented in `SPEC.md`. The important production property is the stable `error_code` plus `request_id` for log correlation.
**Trade-off:** This is less standard than `application/problem+json`. In a real public API I would migrate to problem+json early, before clients depend on the simpler shape. For the take-home, documenting the deliberate choice is better than pretending it was implemented.

### Idempotency Inside the Transaction
**Decision (mine):** The idempotency key lookup is the first statement inside `async with conn.transaction()`, before the `FOR UPDATE` lock.  
**Why:** If the lookup is outside the transaction, two concurrent requests with the same key can both find no cached entry, both proceed to execute, and one fails on the UNIQUE constraint. That gives the client an unexpected 500 on a retry — the opposite of what idempotency is supposed to guarantee.  
**Trade-off:** None. This is strictly correct. The AI's first draft put the lookup outside the transaction; I moved it in.

### Spread Applied as `mid × (1 − spread)` Only
**Decision (mine):** The customer always gets a rate below mid. The "buy" and "sell" rates exposed in `/rates` are for display; the `effective_rate` function is what drives all calculations.  
**Why:** Simpler and less error-prone than maintaining separate buy/sell rate tables and choosing the correct side per conversion direction. The economics are identical.  
**Trade-off:** The `/rates` snapshot shows buy/sell/mid for transparency, but the bid-ask framing is cosmetic.

### Redis for Shared Rate Cache
**Decision (mine):** Redis as a distributed rate cache across all API workers.  
**Why:** The `RateProvider` keeps an in-memory dict for zero-latency reads. With a single worker this is fine. With multiple workers each refreshes independently from the live API — they diverge slightly in timing, serve different rates to the same customer depending on which worker handles the request, and all hit the external API in parallel. Redis fixes this: on every successful refresh, one worker writes the 12 mid-rates to a `fx:rates:mids` key. Other workers, on startup or after a failed API call, read from Redis rather than hitting the live API. Rates stay consistent across the fleet.  
**Failure mode:** If Redis is unavailable, `RateProvider` falls back to its in-memory cache (or seed data on first start). Redis is not in the critical path — a Redis outage degrades but does not break FX operations.  
**TTL:** The Redis key TTL is set to `rate_stale_seconds + 120` (stale threshold + 2-minute buffer). This ensures the cache outlives the staleness window — workers can read stale-but-valid rates from Redis while a refresh is in flight, rather than all hammering the API simultaneously.  
**Trade-off:** Redis adds an infrastructure dependency. The benefit — rate consistency across workers — only matters at scale. For a single-worker deployment it's unnecessary. Acceptable: the assignment targets production-readiness, and production means multiple workers.

### Atomicity Test Hook (`_after_debit_hook`)
**Decision (mine):** A module-level no-op function `_after_debit_hook()` is called inside `execute_quote` between the debit UPDATE and the credit UPDATE. Tests override it via `monkeypatch` to raise an exception, proving that a mid-execute failure rolls back both balance updates and leaves no transaction row.  
**Why:** A statement like "execution is atomic" is unverifiable from the outside — the test just sees a 500 and a clean state. The hook makes the failure point deterministic and reproducible. The test asserts: after the hook raises, `balances` are unchanged, `transactions` has zero rows for this quote, and `ledger_entries` has zero rows. That is the only way to prove the atomicity invariant is real, not incidental.  
**What I rejected:** Patching the database connection itself. That tests the mock, not the engine. The hook is in production code, not test code — it is the real code path that runs in production, with a no-op implementation.

### RabbitMQ for Event Publishing
**Decision (mine):** RabbitMQ with a durable topic exchange (`fx.events`) over Redis Pub/Sub, Kafka, or direct HTTP webhooks.  
**Why RabbitMQ over Kafka:** Kafka is designed for millions of events per second with multiple independent consumer groups replaying the same stream. Umba's FX volume is thousands of transactions per day — Kafka would be over-engineering. RabbitMQ is designed for exactly the use cases here: "quote executed → notify customer", "quote expired → trigger audit", "quote created → update ledger". These are task-queue and routing patterns, not stream-processing patterns. RabbitMQ's management UI (port 15672) also gives immediate visibility into message flow during development.  
**Why RabbitMQ over Redis Pub/Sub:** Redis Pub/Sub is fire-and-forget with no persistence — if a consumer is offline when the event is published, the message is lost. RabbitMQ persists messages (durable exchange + persistent delivery mode) and supports dead-letter queues for retry on consumer failure.  
**Topic exchange with routing keys:** Using `TOPIC` exchange type with routing keys `quote.created`, `quote.executed`, `quote.expired` means any number of consumers can bind to the exchange with different patterns (`quote.*` for all, `quote.executed` for just transactions). No code changes are needed to add a new consumer.  
**Fire-and-forget pattern:** Publishing happens after the database transaction commits, never inside it. If the publish fails (broker offline, network error), the event is logged and dropped — the FX operation has already committed and must not be rolled back. In production, the Outbox pattern would guarantee delivery: write events to a DB table inside the transaction, relay to RabbitMQ via a separate process. This is documented as future work.  
**Trade-off:** Events can be lost if RabbitMQ is down at publish time. Acceptable for this scope given the Outbox pattern is the documented next step.

---

## What I Delegated vs. Owned

| Decision | Who |
|----------|-----|
| FastAPI over Flask | Me |
| PostgreSQL + asyncpg | Me |
| Alembic over custom migration runner | Me |
| Migration as deploy step (not runtime) | Me |
| `db/` folder naming | Me |
| Composite index selection and rationale | Me |
| Redis for shared rate cache | Me |
| RabbitMQ over Kafka / Redis Pub/Sub | Me |
| Fire-and-forget event publishing (after commit, not inside) | Me |
| Redis TTL = stale_seconds + 120s buffer | Me |
| Idempotency inside transaction | Me (caught AI's initial mistake) |
| Deadlock ordering (alphabetical currency) | Me |
| Rate pre-computation strategy | Me |
| Docker Compose service configuration | Delegated; I reviewed healthcheck params, volumes, service ordering |
| GitHub Actions workflow structure | Delegated; I reviewed the gate job logic |
| Hypothesis strategy parameters | Delegated; I verified `allow_nan=False` and `min_value` bounds |
| structlog processor chain | Delegated; I reviewed processor order and log output format |
| Grafana dashboard JSON | Delegated; I reviewed PromQL queries against actual metric names |
| Alembic `env.py` configuration | Delegated; I reviewed URL prefix handling and psycopg2 vs asyncpg separation |

---

## What I Rejected from the AI

1. **Float arithmetic for rate calculation.** The first draft of `generate_quote` used `float(amount) * float(rate)`. Rejected immediately — this is the same bug planted in `planted_bugs/fx.py:60`.  
2. **`threading.Lock()` for concurrency.** Rejected in favor of `SELECT FOR UPDATE`. A module-level lock is invisible to PostgreSQL and fails across workers.  
3. **Re-fetching the live rate at execution time.** The initial `execute_quote` draft called `_effective_rate()` again. Rejected — the locked-in rate is the entire value proposition of a quote.
4. **Idempotency lookup outside the transaction.** Moved inside.
5. **Idempotency table that did not store the replay payload.** The generated docstring and migration described `response_payload`, but the code only marked the key completed. Fixed by storing the serialized response snapshot on completion; the route still returns the transaction-shaped response for compatibility with the existing API schema.
6. **Same-key in-flight retries continuing into execution.** If `completed_at` was null, the generated path did not stop the second request. Fixed by returning `execution_in_progress` rather than racing.
7. **A rate audit table with no writes.** `rate_snapshots` existed, but refresh never inserted rows. Fixed by persisting 12 rows after every successful refresh and testing the delta.
8. **`BaseHTTPMiddleware` for observability.** Spawns anyio task groups that bind futures to a different event loop than asyncpg's pool. Replaced with a raw ASGI class.
9. **`asyncio_default_fixture_loop_scope = "session"` globally.** Fixtures run on the session loop, test functions run on function loops — asyncpg pool mismatch. Reverted to function scope.
10. **Placing `idx_customers_country` in migration 002.** That column doesn't exist until migration 003. Caught by running `alembic upgrade head` locally — the index creation failed. Moved to 003.
11. **Kafka for event publishing.** Kafka is stream processing at millions of events per second — over-engineering for Umba's FX volume. RabbitMQ is the correct tool for task-queue and routing patterns at this scale.
12. **Redis Pub/Sub for events.** No persistence — messages are lost if a consumer is offline. RabbitMQ with durable exchange and persistent delivery guarantees messages survive consumer restarts.
13. **Publishing inside the database transaction.** If publish fails, the event would trigger a rollback of an already-correct financial operation. Events publish after commit, fire-and-forget.

---

## What I Did Not Trust Without Verifying

- **asyncpg numeric type handling** — tested explicitly that `Decimal(str(row["amount"]))` round-trips correctly through the `set_type_codec` configuration.  
- **`ORDER BY currency FOR UPDATE` deadlock prevention** — verified PostgreSQL acquires locks in the order rows are returned, so `ORDER BY` guarantees a stable lock order.  
- **Hypothesis generating edge-case Decimals** — confirmed `allow_nan=False, allow_infinity=False` prevent false-positive failures from non-numeric inputs.  
- **GitHub Actions `services` PostgreSQL health-check** — tested that the `pg_isready` health check in the workflow correctly gates the test job start.
- **Alembic migration ordering** — ran `alembic upgrade head` locally before every push. Migration 002 originally referenced the `country` column before it existed (added in 003); caught by the local run, fixed before committing.
- **psycopg2 URL format for Alembic** — verified the `postgresql+asyncpg://` → `postgresql://` prefix substitution in the Alembic env file handles both URL forms correctly.
- **Composite index column order** — verified that `(customer_id, status, created_at DESC)` supports the `WHERE customer_id = $1 AND status = $2 ORDER BY created_at DESC` query pattern through PostgreSQL's index scan planner.
- **Rate snapshot audit writes** — the first test asserted the table had exactly 12 rows, but startup refreshes had already written snapshots. I corrected the test to assert that a refresh adds exactly 12 rows, which is the real invariant.
- **Final catch-up suite** — after the idempotency, ledger, readiness, and rate-audit fixes, I ran the focused suite (`test_idempotency.py`, `test_execute.py`, `test_health.py`, `test_rates.py`) and then the full suite. Final result: 85 tests passing.

---

## Final Cross-Check Findings

After the core build was working, I did a final internal audit specifically looking for places where the documentation claimed production semantics that the code did not yet enforce. That pass found four important gaps.

1. **Idempotency replay payload existed in docs but not in code.**  
   `idempotency_keys.response_payload` was documented, but completion only set `completed_at` and `status_code`. Replay still reads the `transactions` row to preserve the current response model, but the idempotency row now stores the JSONB response snapshot it promised to store. That makes the audit trail honest and leaves a clean path to byte-for-byte response replay.

2. **In-flight idempotency rows were not respected.**  
   If a row existed with `completed_at IS NULL`, the path continued toward execution. That could let same-key concurrent retries compete instead of being treated as one logical request. I added `ExecutionInProgressError` and now return a clean 409 for the same key while the original request is unresolved.

3. **Rate audit provenance was only half-built.**  
   `rate_snapshots` existed from the initial schema, but `RateProvider` only updated memory and Redis. I added snapshot persistence on successful refresh and a regression test that each refresh adds exactly 12 snapshot rows. I intentionally did not add full `rate_refreshes/current_rates/quote_legs`; that is a larger audit model and is documented as a production extension.

4. **Error format was simpler than RFC 7807.**  
   The final audit highlighted that the API was not using `application/problem+json`. I chose not to bolt on RFC 7807 late. Instead I documented the actual wire contract in `SPEC.md`: stable `error_code`, human `error`, and `request_id`. The important part is honesty: the docs now match the implementation.

The lesson is the same as the planted-bugs review: AI can produce code and docs that agree in tone but not in fact. The final pass was useful because it tested the claims, not just the implementation.

---

## One Thing the AI Got Wrong

The first draft of `execute_quote` placed the idempotency key lookup **outside** the database transaction — before `async with conn.transaction():` opened.

The consequence: two concurrent retries with the same key both execute the `SELECT FROM transactions WHERE idempotency_key = $1` check, both find nothing, both fall through to execution. One commits. The other hits the UNIQUE constraint on `idempotency_key` and returns a 500 — not a clean idempotent replay, a server error on a client retry. That is the exact opposite of what idempotency is supposed to guarantee.

I caught it by reading the function top-to-bottom before committing and cross-referencing it against the idempotency constraint in my spec. The fix was moving the key lookup to be the first statement *inside* `async with conn.transaction():`, before the `SELECT ... FOR UPDATE` lock on the quote row. Inside the transaction, the lookup and the eventual insert are serialised — a second concurrent caller either finds the cached row and short-circuits, or waits behind the row lock and then also finds it.

This class of bug — a check-then-act race where the check and the act are in different transaction scopes — is structurally identical to bugs 1 and 5 in `planted_bugs/`. LLMs reproduce common code patterns, and common patterns in financial systems contain exactly these races.

---

## What I'd Do with Another Day

1. **Outbox pattern for guaranteed event delivery** — events are fire-and-forget after commit. Write them to an `outbox` table in the same transaction, relay via a background process. Only way to guarantee exactly-once delivery without distributed transactions.
2. **Full rate refresh audit model** — add `rate_refreshes` and `current_rates` tables, plus `quote_legs` for per-leg quote provenance. The current implementation persists `rate_snapshots`; the fuller model would make refresh attempts and multi-leg pricing rebuildable end to end.
3. **Balance reservation on quote generation** — reserve `from_amount` at quote time, release on expiry or execute. Prevents a customer from holding 10 quotes backed by the same funds.
4. **Quote expiry background worker** — proactively sweep and mark expired quotes, enabling accurate `expired` vs `abandoned` reporting and releasing balance reservations on time.
5. **Reverse quote** — accept `to_amount`, back-calculate `from_amount = to_amount / effective_rate`. Essential for remittance: "how much do I send to deliver exactly 50,000 KES?"
6. **Cursor pagination on list endpoints** — `GET /transactions`, `GET /quotes`, `GET /customers` return unbounded sets. Add `?cursor=` + `?limit=`.
7. **KYC enforcement on execute** — field and constraint exist; enforcement waits on the auth layer.
8. **Per-customer spread tiers** — `tier` column (`standard`/`premium`), tighter spreads for premium. `get_effective_rate()` already accepts a spread parameter; wiring in customer tier is a small change with direct revenue impact.
