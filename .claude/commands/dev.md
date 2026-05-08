# Start Development Environment

Starts the full local development stack.

## Option A — API only (uses existing test DB on port 5433)
```bash
cd fx_engine && uvicorn app.main:app --reload --port 8000
```
Requires `.env` to have `DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db`

## Option B — Full stack via Docker (API + dedicated DB)
```bash
cd fx_engine && docker compose up --build
```
API runs on port 8000. PostgreSQL runs on port 5432 internally.

## Useful endpoints once running
- Swagger UI:   http://localhost:8000/docs
- Health check: http://localhost:8000/healthz
- Live rates:   http://localhost:8000/rates
- Metrics:      http://localhost:8000/metrics
