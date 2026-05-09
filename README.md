# FX Engine

A production-quality foreign exchange API supporting USD, EUR, KES, and NGN with per-customer balance accounts, KYC tracking, event publishing, and live observability. Built with FastAPI, PostgreSQL, asyncpg, Redis, RabbitMQ, and Prometheus/Grafana.

## Architecture at a Glance

![FX Engine Architecture — FastAPI · PostgreSQL · Redis · RabbitMQ · Grafana](image.png)


## Stack

| Layer | Technology |
|---|---|
| API framework | FastAPI 0.115 + uvicorn |
| Database | PostgreSQL 16 via asyncpg |
| Schema migrations | Alembic (versioned, deploy-time) |
| Cache | Redis 7 |
| Event bus | RabbitMQ 3 (AMQP, aio-pika) |
| Observability | Prometheus + Grafana |
| Validation | Pydantic v2 |
| Logging | structlog (JSON) |


## Quick Start

### Full stack with Docker Compose

```bash
cd fx_engine
docker compose up --build
```

Starts: PostgreSQL, Redis, RabbitMQ, API (migrations run automatically on startup), Prometheus, Grafana.

| Service | URL | Credentials |
|---|---|---|
| API | http://localhost:8000 | — |
| Grafana dashboard | http://localhost:3000 | admin / admin |
| RabbitMQ management | http://localhost:15672 | fx / fx_secret |
| Prometheus | http://localhost:9090 | — |

Swagger docs are available at `http://localhost:8000/docs` in the default Docker Compose setup. Set `ENVIRONMENT=production` for a production-like run where `/docs`, `/redoc`, and `/openapi.json` are disabled.

### Local Development (API only)

```bash
cd fx_engine

# Start the test database
docker compose -f docker-compose.test.yml up -d

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — set DATABASE_URL to your running PostgreSQL instance

# Run migrations
DATABASE_URL=<your-db-url> alembic upgrade head

# Start the API
uvicorn app.main:app --reload --port 8000
```

API available at `http://localhost:8000`. Docs at `http://localhost:8000/docs` (development mode).


## Database Migrations

Migrations are managed with Alembic and run automatically in Docker (`alembic upgrade head` is the first command in the Dockerfile CMD). To run manually:

```bash
cd fx_engine

# Apply all pending migrations
DATABASE_URL=postgresql://user:pass@host/db alembic upgrade head

# View migration history
DATABASE_URL=... alembic history

# Roll back one migration
DATABASE_URL=... alembic downgrade -1

# Create a new migration
alembic revision -m "describe_what_changes"
```


## Running Tests

```bash
cd fx_engine

# Start the test database
docker compose -f docker-compose.test.yml up -d

# Run full suite
TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db \
  pytest -v --cov=app
```

### Test categories

| File | What it covers |
|---|---|
| `test_quotes.py` | Quote generation for all 12 pairs, rate lock at generation time, validation |
| `test_execute.py` | Atomic two-leg balance update, expiry, insufficient funds, cross-customer isolation |
| `test_concurrency.py` | N concurrent executes on the same quote → exactly 1 succeeds (SELECT FOR UPDATE proof) |
| `test_idempotency.py` | Retry safety, concurrent retries with same key produce exactly one DB write |
| `test_precision.py` | Hypothesis property tests — 500 examples per pair, monotonicity, spread direction |
| `test_rates.py` | Staleness detection, Redis fallback, refresh failure handling, spread correctness |
| `test_customers.py` | CRUD, KYC status update, balance credit and accumulation |
| `test_health.py` | `/healthz` component status, `/metrics` Prometheus counter presence |

### Concurrency test proof

```
test_concurrent_execute_only_one_succeeds:
  10 concurrent execute_quote calls, each with its own DB connection
  → 1 success, 9 QuoteAlreadyExecutedError (409)
  → transactions table: exactly 1 row for this quote_id ✓

test_concurrent_execute_balance_debited_exactly_once:
  8 concurrent executes on the same USD→EUR quote
  → USD balance = 1400.00 (debited exactly 100.00 once) ✓
```


## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | DB + rate freshness health check |
| `GET` | `/metrics` | Prometheus counters and latency histogram |
| `POST` | `/customers` | Create customer — accepts `name`, `email`, `phone`, `country` |
| `GET` | `/customers` | List all customers |
| `GET` | `/customers/{id}` | Get customer with KYC status |
| `PATCH` | `/customers/{id}/kyc` | Update KYC status (`pending` / `verified` / `rejected`) |
| `GET` | `/customers/{id}/balances` | All currency balances |
| `POST` | `/customers/{id}/credit` | Credit a balance (internal fixture) |
| `POST` | `/quotes` | Generate FX quote — accepts optional `reference` |
| `GET` | `/quotes` | List all quotes — filter by `?customer_id=` |
| `GET` | `/quotes/{id}` | Get single quote |
| `POST` | `/quotes/{id}/execute` | Execute quote atomically, `Idempotency-Key` header supported |
| `GET` | `/transactions` | List all transactions — filter by `?customer_id=` |
| `GET` | `/transactions/{id}` | Get single transaction |
| `GET` | `/rates` | Live rates — 12 pairs with `mid`, `buy`, `sell`, `spread_pct` |
| `POST` | `/rates/refresh` | Force upstream rate refresh |
| `GET` | `/docs` | Swagger UI — interactive testing (development mode only) |


### Customers

```bash
# Create customer (with optional phone and country)
curl -X POST http://localhost:8000/customers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Alice Wanjiku",
    "email": "alice@umbafinance.com",
    "phone": "+254712345678",
    "country": "KE"
  }'

# List all customers
curl http://localhost:8000/customers

# Get customer
curl http://localhost:8000/customers/{customer_id}

# Update KYC status
curl -X PATCH http://localhost:8000/customers/{customer_id}/kyc \
  -H "Content-Type: application/json" \
  -d '{"kyc_status": "verified"}'

# Get balances
curl http://localhost:8000/customers/{customer_id}/balances

# Credit a balance (internal fixture — not for production use)
curl -X POST http://localhost:8000/customers/{customer_id}/credit \
  -H "Content-Type: application/json" \
  -d '{"currency": "USD", "amount": "1000.00"}'
```

### Quotes

```bash
# Generate a quote (with optional client reference)
curl -X POST http://localhost:8000/quotes \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "{customer_id}",
    "from_currency": "USD",
    "to_currency": "KES",
    "amount": "500.00",
    "reference": "INV-2026-001"
  }'

# List all quotes (optionally filtered by customer)
curl "http://localhost:8000/quotes?customer_id={customer_id}"

# Get a single quote
curl http://localhost:8000/quotes/{quote_id}
```

Response:
```json
{
  "quote_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "customer_id": "...",
  "from_currency": "USD",
  "to_currency": "KES",
  "from_amount": "500.00",
  "to_amount": "64100.61",
  "rate": "128.2012",
  "mid_rate": "129.1700",
  "reference": "INV-2026-001",
  "expires_at": "2026-05-08T12:01:00Z",
  "created_at": "2026-05-08T12:00:00Z"
}
```

`mid_rate` is the market mid-rate at quote time. The spread applied = `(mid_rate - rate) / mid_rate`.

```bash
# Execute a quote
curl -X POST http://localhost:8000/quotes/{quote_id}/execute \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"customer_id": "{customer_id}"}'
```

### Transactions

```bash
# List all transactions (optionally filtered by customer)
curl "http://localhost:8000/transactions?customer_id={customer_id}"

# Get a single transaction
curl http://localhost:8000/transactions/{transaction_id}
```

### Rates and Observability

```bash
# Live rates (12 pairs with mid/buy/sell/spread_pct)
curl http://localhost:8000/rates

# Force rate refresh from upstream API
curl -X POST http://localhost:8000/rates/refresh

# Health check
curl http://localhost:8000/healthz

# Prometheus metrics
curl http://localhost:8000/metrics
```

## Example Log Output

Every log line is structured JSON with a request-scoped `request_id` linking related events:

```json
{"event": "startup", "environment": "development", "level": "info", "timestamp": "2026-05-08T12:00:00Z"}
{"event": "rates_refreshed", "pairs": 12, "source": "api", "level": "info", "timestamp": "2026-05-08T12:00:01Z"}
{"event": "customer_created", "customer_id": "abc...", "email": "alice@umbafinance.com", "country": "KE", "request_id": "7b4c2...", "level": "info", "timestamp": "2026-05-08T12:00:03Z"}
{"event": "request_completed", "method": "POST", "path": "/customers", "status_code": 201, "duration_ms": 11.2, "request_id": "7b4c2...", "level": "info", "timestamp": "2026-05-08T12:00:03Z"}
{"event": "quote_created", "quote_id": "3fa85f64...", "pair": "USD/KES", "from_amount": "500.00", "to_amount": "64100.61", "rate": "128.2012", "mid_rate": "129.1700", "request_id": "1a3d9...", "level": "info", "timestamp": "2026-05-08T12:00:05Z"}
{"event": "quote_executed", "transaction_id": "9fe1...", "quote_id": "3fa85f64...", "pair": "USD/KES", "from_amount": "500.00", "to_amount": "64100.61", "request_id": "c8b7e...", "level": "info", "timestamp": "2026-05-08T12:00:15Z"}
```

## Supported Currency Pairs

All 12 combinations of USD, EUR, KES, NGN:

| From \ To | USD | EUR | KES | NGN |
|---|---|---|---|---|
| **USD** | — | ✓ 0.30% | ✓ 0.75% | ✓ 1.00% |
| **EUR** | ✓ 0.30% | — | ✓ 0.75% | ✓ 1.00% |
| **KES** | ✓ 0.75% | ✓ 0.75% | — | ✓ 1.50% |
| **NGN** | ✓ 1.00% | ✓ 1.00% | ✓ 1.50% | — |

Spread percentages shown. All 12 pairs derived at refresh time from three USD-base rates (EUR, KES, NGN).

## Project Structure

```
fx_takehome/
├── README.md                   # This file
├── SPEC.md                     # Full technical specification
├── DECISIONS.md                # Architecture trade-offs and AI delegation log
├── AGENTS.md                   # Agent instructions used during development
├── REVIEW.md                   # Code review findings for planted_bugs/
├── ASSIGNMENT.md               # Original assignment brief
├── image.png                   # Architecture diagram
├── planted_bugs/               # AI-generated baseline code (Part 3 review target)
└── fx_engine/                  # Production API — all active development lives here
    ├── Dockerfile
    ├── docker-compose.yml      # Full stack: API + PostgreSQL + Redis + RabbitMQ + monitoring
    ├── docker-compose.test.yml # Isolated test PostgreSQL on port 5433
    ├── alembic.ini
    ├── pyproject.toml          # Pytest config, ruff linting rules, dependency metadata
    ├── requirements.txt
    ├── .env.example
    │
    ├── app/                    # Application source
    │   ├── main.py             # FastAPI app, raw ASGI middleware, lifespan, error handlers
    │   ├── config.py           # Settings via pydantic-settings (reads .env)
    │   ├── exceptions.py       # FXError hierarchy — maps to HTTP status + error_code
    │   │
    │   ├── engine/
    │   │   └── fx.py           # Core business logic: generate_quote, execute_quote
    │   │
    │   ├── models/
    │   │   ├── customer.py     # CustomerCreate, CustomerResponse, BalanceItem, CreditRequest
    │   │   ├── quote.py        # QuoteRequest, QuoteResponse, ExecuteRequest
    │   │   ├── transaction.py  # TransactionResponse
    │   │   └── shared.py       # RatesResponse, HealthResponse, supported currencies
    │   │
    │   ├── providers/
    │   │   └── rates.py        # RateProvider singleton — live rates, spread model, staleness guard
    │   │
    │   ├── routes/
    │   │   ├── customers.py    # /customers, /customers/{id}, /customers/{id}/balances, /credit
    │   │   ├── quotes.py       # /quotes, /quotes/{id}, /quotes/{id}/execute
    │   │   ├── transactions.py # /transactions, /transactions/{id}
    │   │   ├── rates.py        # /rates, /rates/refresh
    │   │   └── health.py       # /healthz, /metrics
    │   │
    │   └── services/
    │       ├── database.py     # asyncpg pool — get_pool, get_connection, close_pool
    │       ├── cache.py        # Redis client (optional — degrades gracefully if absent)
    │       ├── events.py       # RabbitMQ event publishing (fire-and-forget after commit)
    │       └── metrics.py      # Prometheus counters and request-duration histogram
    │
    ├── db/                     # Alembic migration environment
    │   └── versions/
    │       ├── be56cd133310    # Initial schema: customers, balances, quotes, transactions, rate_snapshots
    │       ├── 1076c0e1bb3f    # Compound indexes for common query patterns
    │       └── aadaaac35570    # Customer enrichment (phone, country, kyc_status) + quote audit fields
    │
    ├── tests/
    │   ├── conftest.py         # Fixtures: client, customer, funded_customer, pending_quote
    │   ├── test_quotes.py      # Quote generation, rate lock, all 12 currency pairs
    │   ├── test_execute.py     # Atomic execution, expiry, balance checks, cross-customer isolation
    │   ├── test_concurrency.py # SELECT FOR UPDATE proof — exactly 1 winner under N concurrent requests
    │   ├── test_idempotency.py # Idempotency-Key header, concurrent retries write exactly one row
    │   ├── test_precision.py   # Hypothesis property tests — Decimal arithmetic, spread direction
    │   ├── test_rates.py       # Staleness enforcement, refresh failure handling, spread correctness
    │   ├── test_customers.py   # CRUD, KYC status update, balance credit and accumulation
    │   ├── test_transactions.py# GET /transactions list + filter, GET /transactions/{id}
    │   └── test_health.py      # /healthz component status, /metrics Prometheus format
    │
    └── monitoring/
        ├── prometheus/
        │   └── prometheus.yml          # Scrape config — targets the API at port 8000
        └── grafana/
            ├── dashboards/
            │   └── fx-engine.json      # Pre-built dashboard: request rate, error rate, latency P95
            └── provisioning/
                ├── dashboards/         # Auto-provisions the dashboard on container start
                └── datasources/        # Wires Prometheus as the default datasource
```


## Known Limitations

- No authentication or authorization (out of scope per assignment). The `/customers/{id}/credit` and `/customers/{id}/kyc` endpoints are unprotected.
- Rate source is `v6.exchangerate-api.com` with the provided API key. Key is read from `RATE_API_KEY` environment variable — never committed to the repository.
- KYC status is stored and updatable but not enforced on execute. Enforcement requires an auth layer to identify the caller.
- `rate_snapshots` table exists for full rate audit history but is not written to on refresh. Each rate object currently stores `mid_rate` at quote generation time for per-quote auditability.
- Outbox pattern not implemented for RabbitMQ — events are fire-and-forget after commit. Under a broker outage, events can be lost. The Outbox pattern (write event to DB table in the same transaction, relay via a separate process) is the production next step.
- Single-region deployment; no cross-region consistency guarantees.
- `/docs` and `/redoc` are disabled when `ENVIRONMENT=production`.


## What I'd Do with Another Day

1. **Outbox pattern for guaranteed event delivery** — events are currently fire-and-forget after commit. Write them to an `outbox` table inside the same transaction, relay via a background process. The only way to guarantee exactly-once delivery without distributed transactions.
2. **Balance reservation on quote generation** — reserve `from_amount` at quote time, release on expiry or execute. Prevents a customer from holding 10 quotes backed by the same funds.
3. **Quote expiry background worker** — proactively sweep and mark expired quotes rather than waiting for a client to attempt execution. Enables accurate reporting and timely reservation release.
4. **Reverse quote** — accept `to_amount` and back-calculate `from_amount` including spread. Essential for remittance: "how much do I send to deliver exactly 50,000 KES?"
5. **Cursor pagination on list endpoints** — `GET /transactions`, `GET /quotes`, and `GET /customers` currently return unbounded result sets. Add `?cursor=` + `?limit=` for production-safe paging.
6. **Per-customer FX limits** — `daily_limit_usd` on the customer table, enforced in `execute_quote`. Required by CBK microfinance regulation for transaction limits per customer tier.
7. **KYC enforcement on execute** — reject execute if `kyc_status != 'verified'`. Field and constraint exist; enforcement waits on the auth layer.
8. **Per-customer spread tiers** — `tier` column (`standard`/`premium`), tighter spreads for premium accounts. `get_effective_rate()` already accepts a spread parameter — wiring in customer tier is a small change with direct revenue impact.


## Time Budget

- Architecture design and spec writing: ~1 hour
- Core implementation (engine, routes, tests): ~5 hours
- Production hardening (Redis, RabbitMQ, Grafana, Alembic, schema enrichment): ~4 hours
- Code review (`planted_bugs/`): ~1.5 hours
- Documentation (SPEC, DECISIONS, AGENTS, README): ~1.5 hours
- Total wall-clock: ~14 hours across 2 days
