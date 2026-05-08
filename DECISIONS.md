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

### `db/` Folder for Migrations
**Decision (mine):** `db/versions/` for Alembic revision files, `alembic.ini` at the project root.  
**Why:** `migrations/` is too generic — it doesn't signal ownership or tooling. `db/` communicates "everything database schema lives here." Alembic's `alembic.ini` stays at the root by convention (`alembic upgrade head` expects it there by default); moving it requires passing `-c` everywhere.  
**Trade-off:** `alembic.ini` at root and scripts in `db/` is a slight split, but it follows Alembic's own documented recommended layout.

### Three Migrations — Rationale for Each
**001 initial_schema:** Full schema in one migration. It was the only migration at the time and there was nothing to separate it from.  
**002 compound_indexes:** Separated from the initial schema deliberately — indexes are tuning decisions, not structural decisions. Separating them makes it easy to see when index strategy changed and to roll back index changes without touching table definitions. The composite indexes chosen:
- `quotes(customer_id, status, created_at DESC)` — covers "show me this customer's pending quotes, newest first" — the primary dashboard query pattern.
- `transactions(customer_id, executed_at DESC)` — covers account statement queries.
- `idx_quotes_status_expires` from the initial schema — covers the background expired-quote cleanup query.  

The original single-column `idx_quotes_customer` was dropped — it's subsumed by the composite index and would just waste write overhead.  
**003 customer enrichment + quote audit fields:** Schema changes that reflect product decisions, kept separate from structural (001) and tuning (002) changes. `phone` and `country` on customers are Umba-specific — Kenya is M-Pesa driven, country is required for regulatory reporting. `kyc_status` is a first-class financial compliance concept. `reference` on quotes is a standard payments pattern (client reconciliation ref). `mid_rate` on quotes enables auditing the exact spread applied to any historical quote.

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

### `monitoring/` Folder for Observability Configs
**Decision (mine):** `monitoring/prometheus/prometheus.yml` and `monitoring/grafana/` over bare `prometheus.yml` at root and `grafana/` as a top-level folder.  
**Why:** A folder named after a tool (`grafana/`) exposes an implementation detail rather than a concept. `monitoring/` names the concern. A reviewer sees `monitoring/` and immediately knows this is where operational configuration lives, regardless of which specific tools are used. Swapping Grafana for another dashboard tool in the future only changes what's inside `monitoring/`, not the folder name.  
**Trade-off:** One extra level of nesting for `prometheus.yml` (`monitoring/prometheus/prometheus.yml`). Worth it for conceptual clarity.

### App Folder Structure — `models/`, `routes/`, `engine/`, `providers/`, `services/`
**Decision (mine):** Five clearly-named top-level directories inside `app/`, each with an unambiguous responsibility.  
**Why each name was chosen:**

| Folder | Rule | What's inside |
|---|---|---|
| `models/` | What data looks like | Pydantic schemas split by domain (customer, quote, transaction, shared) |
| `routes/` | How requests arrive | HTTP transport only — thin, delegates to engine/services |
| `engine/` | What the system does | FX business logic: generate_quote, execute_quote |
| `providers/` | External data we pull | RateProvider calls a third-party exchange rate API |
| `services/` | Infrastructure we own | Database pool, Redis, RabbitMQ, Prometheus |

**The key distinction: `providers/` vs `services/`**  
`RateProvider` was initially in `services/`. That's wrong. A service adapter is something we control (our PostgreSQL, our Redis). A provider is an external data source we depend on (exchangeratesapi.io). Moving `rates.py` to `providers/` communicates this ownership boundary explicitly. When exchangeratesapi.io's API changes, you look in `providers/`. When the database pool needs tuning, you look in `services/`.

**Why not feature-based (e.g. `quotes/`, `customers/`)?**  
Feature folders make sense when each feature has its own models, routes, and service logic. Here, all business logic lives in one place (`engine/fx.py`) because the execute path crosses both quote and balance domains — splitting by feature would require either duplication or circular imports.

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
| `monitoring/` folder naming over `grafana/` | Me |
| `models/routes/engine/providers/services` structure | Me |
| `providers/` vs `services/` distinction | Me |
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
8. **`grafana/` as a top-level folder.** Names a tool, not a concept. Renamed to `monitoring/`.
9. **All schemas in one `schemas.py` file.** Split into `models/customer.py`, `models/quote.py`, `models/transaction.py`, `models/shared.py` — each domain in its own file.

---

## What I Did Not Trust Without Verifying

- **asyncpg numeric type handling** — tested explicitly that `Decimal(str(row["amount"]))` round-trips correctly through the `set_type_codec` configuration.  
- **`ORDER BY currency FOR UPDATE` deadlock prevention** — verified PostgreSQL acquires locks in the order rows are returned, so `ORDER BY` guarantees a stable lock order.  
- **Hypothesis generating edge-case Decimals** — confirmed `allow_nan=False, allow_infinity=False` prevent false-positive failures from non-numeric inputs.  
- **GitHub Actions `services` PostgreSQL health-check** — tested that the `pg_isready` health check in the workflow correctly gates the test job start.
- **Alembic migration ordering** — ran `alembic upgrade head` locally before every push. Migration 002 originally referenced the `country` column before it existed (added in 003); caught by the local run, fixed before committing.
- **psycopg2 URL format for Alembic** — verified the `postgresql+asyncpg://` → `postgresql://` prefix substitution in `db/env.py` handles both URL forms correctly.
- **Composite index column order** — verified that `(customer_id, status, created_at DESC)` supports the `WHERE customer_id = $1 AND status = $2 ORDER BY created_at DESC` query pattern through PostgreSQL's index scan planner.

---

## What I'd Do with Another Day

1. **Rate persistence** — write each refresh's rates to `rate_snapshots` and allow quote generation to reference the snapshot ID, enabling full rate audit trails.
2. **Webhook notifications** — `POST` to a customer-configured URL when a quote is executed.
3. **Quote streaming** — SSE endpoint streaming live rate updates so client UIs don't need to poll.
4. **Per-customer spread tiers** — premium customers get tighter spreads based on `tier` column on the customer table.
5. **Kubernetes manifests** — HPA, PodDisruptionBudget, readiness/liveness probes using `/healthz`.
6. **OpenTelemetry traces** — span the full quote → execute flow with the `trace_id` carried through.
7. **`dev.yml` and `staging.yml` GitHub Actions** — dedicated CI workflows for each environment in the promotion chain (dev: lint + tests; staging: tests + Docker build; production: full E2E pipeline).
8. **KYC enforcement on execute** — reject execute if `kyc_status != 'verified'` once authentication is in place.
