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


### Three Migrations — Rationale for Each

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

### Redis for Shared Rate Cache
**Decision (mine):** Redis as a distributed rate cache across all API workers.  
**Why:** The `RateProvider` keeps an in-memory dict for zero-latency reads. With a single worker this is fine. With multiple workers each refreshes independently from the live API — they diverge slightly in timing, serve different rates to the same customer depending on which worker handles the request, and all hit the external API in parallel. Redis fixes this: on every successful refresh, one worker writes the 12 mid-rates to a `fx:rates:mids` key. Other workers, on startup or after a failed API call, read from Redis rather than hitting the live API. Rates stay consistent across the fleet.  
**Failure mode:** If Redis is unavailable, `RateProvider` falls back to its in-memory cache (or seed data on first start). Redis is not in the critical path — a Redis outage degrades but does not break FX operations.  
**TTL:** The Redis key TTL is set to `rate_stale_seconds + 120` (stale threshold + 2-minute buffer). This ensures the cache outlives the staleness window — workers can read stale-but-valid rates from Redis while a refresh is in flight, rather than all hammering the API simultaneously.  
**Trade-off:** Redis adds an infrastructure dependency. The benefit — rate consistency across workers — only matters at scale. For a single-worker deployment it's unnecessary. Acceptable: the assignment targets production-readiness, and production means multiple workers.

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
| Docker Compose service configuration | AI, reviewed |
| GitHub Actions workflow structure | AI, reviewed |
| Hypothesis strategy parameters | AI, reviewed (verified `allow_nan=False`) |
| structlog processor chain | AI, reviewed |
| Grafana dashboard JSON | AI, reviewed panels and PromQL queries |
| Alembic `env.py` configuration | AI, reviewed (URL prefix handling, psycopg2 vs asyncpg) |

---

## What I Rejected from the AI

1. **Float arithmetic for rate calculation.** The first draft of `generate_quote` used `float(amount) * float(rate)`. Rejected immediately — this is the same bug planted in `planted_bugs/fx.py:60`.  
2. **`threading.Lock()` for concurrency.** Rejected in favor of `SELECT FOR UPDATE`. A module-level lock is invisible to PostgreSQL and fails across workers.  
3. **Re-fetching the live rate at execution time.** The initial `execute_quote` draft called `_effective_rate()` again. Rejected — the locked-in rate is the entire value proposition of a quote.
4. **Idempotency lookup outside the transaction.** Moved inside.
5. **`BaseHTTPMiddleware` for observability.** Spawns anyio task groups that bind futures to a different event loop than asyncpg's pool. Replaced with a raw ASGI class.
6. **`asyncio_default_fixture_loop_scope = "session"` globally.** Fixtures run on the session loop, test functions run on function loops — asyncpg pool mismatch. Reverted to function scope.
7. **Placing `idx_customers_country` in migration 002.** That column doesn't exist until migration 003. Caught by running `alembic upgrade head` locally — the index creation failed. Moved to 003.
8. **Kafka for event publishing.** Kafka is stream processing at millions of events per second — over-engineering for Umba's FX volume. RabbitMQ is the correct tool for task-queue and routing patterns at this scale.
9. **Redis Pub/Sub for events.** No persistence — messages are lost if a consumer is offline. RabbitMQ with durable exchange and persistent delivery guarantees messages survive consumer restarts.
10. **Publishing inside the database transaction.** If publish fails, the event would trigger a rollback of an already-correct financial operation. Events publish after commit, fire-and-forget.

---

## What I Did Not Trust Without Verifying

- **asyncpg numeric type handling** — tested explicitly that `Decimal(str(row["amount"]))` round-trips correctly through the `set_type_codec` configuration.  
- **`ORDER BY currency FOR UPDATE` deadlock prevention** — verified PostgreSQL acquires locks in the order rows are returned, so `ORDER BY` guarantees a stable lock order.  
- **Hypothesis generating edge-case Decimals** — confirmed `allow_nan=False, allow_infinity=False` prevent false-positive failures from non-numeric inputs.  
- **GitHub Actions `services` PostgreSQL health-check** — tested that the `pg_isready` health check in the workflow correctly gates the test job start.
- **Alembic migration ordering** — ran `alembic upgrade head` locally before every push. Migration 002 originally referenced the `country` column before it existed (added in 003); caught by the local run, fixed before committing.
- **psycopg2 URL format for Alembic** — verified the `postgresql+asyncpg://` → `postgresql://` prefix substitution in the Alembic env file handles both URL forms correctly.
- **Composite index column order** — verified that `(customer_id, status, created_at DESC)` supports the `WHERE customer_id = $1 AND status = $2 ORDER BY created_at DESC` query pattern through PostgreSQL's index scan planner.

---

## One Thing the AI Got Wrong

The first draft of `execute_quote` placed the idempotency key lookup **outside** the database transaction — before `async with conn.transaction():` opened.

The consequence: two concurrent retries with the same key both execute the `SELECT FROM transactions WHERE idempotency_key = $1` check, both find nothing, both fall through to execution. One commits. The other hits the UNIQUE constraint on `idempotency_key` and returns a 500 — not a clean idempotent replay, a server error on a client retry. That is the exact opposite of what idempotency is supposed to guarantee.

I caught it by reading the function top-to-bottom before committing and cross-referencing it against the idempotency constraint in my spec. The fix was moving the key lookup to be the first statement *inside* `async with conn.transaction():`, before the `SELECT ... FOR UPDATE` lock on the quote row. Inside the transaction, the lookup and the eventual insert are serialised — a second concurrent caller either finds the cached row and short-circuits, or waits behind the row lock and then also finds it.

This class of bug — a check-then-act race where the check and the act are in different transaction scopes — is structurally identical to bugs 1 and 5 in `planted_bugs/`. LLMs reproduce common code patterns, and common patterns in financial systems contain exactly these races.

---

## What I'd Do with Another Day

1. **Outbox pattern for guaranteed event delivery** — currently events are fire-and-forget after commit; a broker outage silently drops them. The fix: write events to an `outbox` table inside the same database transaction as the execute, then relay to RabbitMQ via a separate background process. This is the only way to guarantee exactly-once delivery without distributed transactions.

2. **Balance reservation on quote generation** — currently a customer can generate 10 quotes for 1,000 USD while only holding 1,000 USD. The first execute succeeds, the rest fail with `insufficient_balance`. A better model: reserve `from_amount` against the customer's balance at quote time, release on expiry or execute. This makes the quote a true commitment from both sides.

3. **Quote expiry background worker** — quotes currently transition to `expired` only when a client attempts to execute them. A background sweep should proactively mark expired quotes, enabling accurate reporting (`expired` vs `abandoned`) and releasing balance reservations on time.

4. **Reverse quote** — "how much USD do I need to send to receive exactly 50,000 KES?" Currently the API only accepts `from_amount`. A reverse quote accepts `to_amount` and back-calculates `from_amount = to_amount / rate`, including the spread. Essential for remittance flows where the recipient amount is fixed.

5. **Transaction history endpoint** — `GET /customers/{id}/transactions` with cursor-based pagination and date range filtering. The `transactions` table exists; the endpoint does not. Required for account statements and customer-facing reconciliation.

6. **Per-customer FX limits** — a `daily_limit_usd` column on the `customers` table, checked inside `execute_quote` before debiting. Microfinance regulation (including CBK guidelines) requires transaction limits per customer tier. This is a one-line schema change and a single balance-check addition to the execute path.

7. **KYC enforcement on execute** — reject execute if `kyc_status != 'verified'`. The field, constraint, and update endpoint already exist. Enforcement requires an authentication layer to identify the caller — first step once auth is introduced.

8. **Per-customer spread tiers** — a `tier` column (`standard`, `premium`) on customers. Premium customers get tighter spreads (e.g. 0.15% on USD/EUR instead of 0.30%). The `get_effective_rate()` function accepts a spread parameter — wiring in customer tier is a small change with direct revenue impact.
