# FX Engine

A production-quality foreign exchange API supporting USD, EUR, KES, and NGN. Built with FastAPI, PostgreSQL, and asyncpg.

## Architecture at a Glance

```
POST /quotes          →  fx_engine.generate_quote()  →  quotes table
POST /quotes/{id}/execute  →  fx_engine.execute_quote()  →  transactions + balances (atomic)
GET  /rates           →  RateProvider.snapshot()
GET  /healthz         →  DB + rates liveness
GET  /metrics         →  Prometheus counters
```

**Concurrency safety:** `SELECT ... FOR UPDATE` on the quote row inside a PostgreSQL transaction. No application-level locks — works correctly across any number of workers.

**Idempotency:** `Idempotency-Key` header. The key lookup is inside the same transaction as the execute, eliminating the TOCTOU race.

**Decimal precision:** All arithmetic in Python `Decimal`. Quantized to 2 dp with `ROUND_HALF_UP` once at quote generation. Float is never used.

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.12+ (for local development)

### Run with Docker Compose

```bash
cd fx_engine
cp .env.example .env
docker compose up --build
```

API available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

### Local Development

```bash
cd fx_engine

# Start PostgreSQL only
docker compose up db -d

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment
cp .env.example .env

# Run the API
uvicorn app.main:app --reload --port 8000
```

---

## API Reference

### Create a Customer
```bash
curl -X POST http://localhost:8000/customers \
  -H "Content-Type: application/json" \
  -d '{"name": "Alice Wanjiku", "email": "alice@example.com"}'
```

### Credit a Balance (test fixture)
```bash
curl -X POST http://localhost:8000/customers/{customer_id}/balances/credit \
  -H "Content-Type: application/json" \
  -d '{"currency": "USD", "amount": "1000.00"}'
```

### Generate a Quote
```bash
curl -X POST http://localhost:8000/quotes \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "{customer_id}",
    "from_currency": "USD",
    "to_currency": "KES",
    "amount": "500.00"
  }'
```

Response:
```json
{
  "quote_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "customer_id": "...",
  "from_currency": "USD",
  "to_currency": "KES",
  "from_amount": "500.00",
  "to_amount": "64267.25",
  "rate": "128.5345",
  "expires_at": "2026-05-08T12:01:00Z",
  "created_at": "2026-05-08T12:00:00Z"
}
```

### Execute a Quote
```bash
curl -X POST http://localhost:8000/quotes/{quote_id}/execute \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"customer_id": "{customer_id}"}'
```

Response:
```json
{
  "transaction_id": "...",
  "quote_id": "...",
  "customer_id": "...",
  "from_currency": "USD",
  "to_currency": "KES",
  "from_amount": "500.00",
  "to_amount": "64267.25",
  "rate": "128.5345",
  "status": "success",
  "executed_at": "2026-05-08T12:00:15Z"
}
```

### Other Endpoints
```bash
# View current rates
curl http://localhost:8000/rates

# Force rate refresh
curl -X POST http://localhost:8000/rates/refresh

# Health check
curl http://localhost:8000/healthz

# Prometheus metrics
curl http://localhost:8000/metrics
```

---

## Running Tests

### With Docker (recommended)

```bash
cd fx_engine

# Start the test database
docker compose -f docker-compose.test.yml up -d

# Wait for it to be healthy, then run tests
TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db \
  pytest -v --cov=app
```

### Test Categories

| File | What it tests |
|------|--------------|
| `test_quotes.py` | Quote generation, all 12 currency pairs, validation |
| `test_execute.py` | Atomicity, balance updates, expiry, insufficient funds, wrong customer |
| `test_concurrency.py` | N parallel executes → exactly 1 succeeds; deadlock-free cross-pair execution |
| `test_idempotency.py` | Retry safety, TOCTOU race under concurrent retries |
| `test_precision.py` | Hypothesis property tests — 500 examples, all pairs, all amount ranges |
| `test_rates.py` | Staleness, fallback, refresh failure, spread direction |
| `test_customers.py` | CRUD, balance credit/accumulation |
| `test_health.py` | /healthz, /metrics |

### Example: Concurrency Test Output

```
tests/test_concurrency.py::test_concurrent_execute_only_one_succeeds PASSED
  20 parallel requests, 1 succeeded, 19 returned 409 (already_executed)
  DB row count for transactions: 1 ✓

tests/test_concurrency.py::test_concurrent_execute_balance_debited_exactly_once PASSED
  15 parallel requests, USD balance = 1400.00 (debited exactly 100.00 once) ✓
```

---

## Example Log Output

```json
{"event": "startup", "environment": "production", "level": "info", "timestamp": "2026-05-08T12:00:00Z"}
{"event": "rates_refreshed", "pairs": 12, "source": "api", "level": "info", "timestamp": "2026-05-08T12:00:01Z"}
{"event": "startup_complete", "level": "info", "timestamp": "2026-05-08T12:00:01Z"}
{"event": "quote_created", "quote_id": "3fa85f64...", "customer_id": "abc...", "pair": "USD/KES", "from_amount": "500.00", "to_amount": "64267.25", "rate": "128.5345", "level": "info", "request_id": "7b4c2...", "timestamp": "2026-05-08T12:00:05Z"}
{"event": "request_completed", "method": "POST", "path": "/quotes", "status_code": 201, "duration_ms": 8.42, "request_id": "7b4c2...", "level": "info", "timestamp": "2026-05-08T12:00:05Z"}
{"event": "quote_executed", "transaction_id": "9fe1...", "quote_id": "3fa85f64...", "customer_id": "abc...", "pair": "USD/KES", "from_amount": "500.00", "to_amount": "64267.25", "level": "info", "request_id": "1a3d9...", "timestamp": "2026-05-08T12:00:15Z"}
```

---

## Supported Currency Pairs

All 12 combinations of USD, EUR, KES, NGN:

| From \ To | USD | EUR | KES | NGN |
|-----------|-----|-----|-----|-----|
| **USD** | — | ✓ | ✓ | ✓ |
| **EUR** | ✓ | — | ✓ | ✓ |
| **KES** | ✓ | ✓ | — | ✓ |
| **NGN** | ✓ | ✓ | ✓ | — |

All rates derived from live USD-base rates (api.exchangerate-api.com). Spreads vary by pair (0.3%–1.5%).

---

## Known Limitations

- No authentication/authorization (out of scope per assignment).
- Rate source is api.exchangerate-api.com free tier — 1,500 requests/month. For production, replace with a paid tier or Bloomberg/Reuters feed.
- `POST /customers/{id}/balances/credit` is an unprotected test fixture; in production this would be an internal admin endpoint behind auth.
- Rate `rate_snapshots` table is populated but not queried — exists for future rate audit use.
- Single-region deployment; no cross-region consistency guarantees.

---

## What I'd Do with Another Day

1. **OpenTelemetry tracing** — distributed spans linking quote → execute with the `X-Request-ID` as the trace root.
2. **Rate audit trail** — surface `rate_snapshots` as a `/rates/history` endpoint.
3. **Per-customer spreads** — premium tier with tighter spreads stored in the customer row.
4. **Kubernetes manifests** — HPA based on request rate, PodDisruptionBudget for zero-downtime deploys.
5. **Webhook notifications** — `POST` to a customer URL on execute, with HMAC signatures.
6. **Quote streaming** — SSE endpoint for live rate updates, eliminating client polling.

---

## Project Structure

```
fx_engine/
├── app/
│   ├── main.py          # FastAPI app, lifespan, middleware, error handlers
│   ├── config.py        # Settings (pydantic-settings, reads .env)
│   ├── database.py      # asyncpg pool, migrations runner
│   ├── fx_engine.py     # Core: generate_quote, execute_quote (atomic)
│   ├── rates.py         # RateProvider: fetch, cache, staleness, spreads
│   ├── schemas.py       # Pydantic v2 request/response models
│   ├── exceptions.py    # Typed exception hierarchy
│   └── routers/
│       ├── quotes.py    # POST /quotes, POST /quotes/{id}/execute
│       ├── customers.py # Customer CRUD, balance credit
│       ├── rates.py     # GET /rates, POST /rates/refresh
│       └── health.py    # GET /healthz, GET /metrics
├── migrations/
│   └── 001_initial.sql  # Idempotent schema
├── tests/               # Full test suite (see above)
├── docker-compose.yml
├── docker-compose.test.yml
├── Dockerfile
└── requirements.txt
```

---

## Time Budget

- Active design and implementation: ~6 hours
- Code review (`planted_bugs/`): ~1.5 hours
- Documentation (SPEC, DECISIONS, AGENTS, README): ~1 hour
- Total wall-clock: ~10 hours across 2 sessions
