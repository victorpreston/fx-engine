# FX Engine — Technical Specification

## Overview

REST API for foreign exchange between USD, EUR, KES, and NGN with per-customer balance accounts. Quotes lock in a rate for 60 seconds; execution is atomic across both currency legs.

---

## Currencies and Minor Units

| Currency | Name | Minor Unit | Output Precision |
|----------|------|-----------|-----------------|
| USD | US Dollar | cent | 2 dp |
| EUR | Euro | cent | 2 dp |
| KES | Kenyan Shilling | cent | 2 dp |
| NGN | Nigerian Naira | kobo | 2 dp |

**Internal storage:** `NUMERIC(20, 8)` — 8 decimal places preserved in the database for accounting precision. Amounts presented to users are quantized to 2 decimal places at the API boundary only.

---

## Rounding Rules

- All intermediate arithmetic uses Python's `decimal.Decimal` module.
- Quantization: `ROUND_HALF_UP` to 2 decimal places.
- Quantization happens **once**: at quote generation for `to_amount`. All subsequent calculations (execution, balance updates) reuse the stored `to_amount` — the rate is never recomputed at execution time.
- Float is never used; all rate and amount values are `Decimal` throughout.

---

## FX Rate Model

### Direct Pairs Stored
USD/EUR, USD/KES, USD/NGN, EUR/USD, EUR/KES, EUR/NGN, KES/USD, KES/EUR, KES/NGN, NGN/USD, NGN/EUR, NGN/KES (12 total, all derived from three USD base rates).

### Spread Model

Each pair has a per-pair spread fraction applied against the customer:

```
effective_rate(A→B) = mid(A/B) × (1 − spread(A/B))
```

The customer always receives slightly less than the mid-rate. The bank retains `spread × from_amount × mid` as revenue on each conversion.

| Pair | Spread |
|------|--------|
| USD/EUR, EUR/USD | 0.30% |
| USD/KES, KES/USD, EUR/KES, KES/EUR | 0.75% |
| USD/NGN, NGN/USD, EUR/NGN, NGN/EUR | 1.00% |
| KES/NGN, NGN/KES | 1.50% |

### Rate Derivation from API

The live rate source provides USD-base rates for EUR, KES, NGN. All 12 pairs are computed from these three values:

```
mid(EUR/KES) = mid(USD/KES) / mid(USD/EUR)
mid(KES/NGN) = mid(USD/NGN) / mid(USD/KES)
... (all 12 derived similarly)
```

### Cross-Pair Routing

All 12 A/B pairs are computed directly (not routed at query time). There is no runtime routing step — the derivation happens at rate-refresh time and all pairs are pre-computed and stored in memory.

---

## Customers

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Unique customer identifier |
| `name` | TEXT | Full name |
| `email` | TEXT | Unique email address |
| `phone` | TEXT | E.164 phone number (e.g. `+254712345678`), optional |
| `country` | CHAR(2) | ISO 3166-1 alpha-2 country code (e.g. `KE`), optional |
| `kyc_status` | enum | `pending`, `verified`, `rejected` — defaults to `pending` |
| `created_at` | TIMESTAMPTZ | Account creation timestamp |

KYC enforcement on execute is **out of scope** for this submission (no auth layer). The field and constraint exist in the database; enforcement is the next step once authentication is introduced.

---

## Quotes

| Field | Type | Description |
|-------|------|-------------|
| `quote_id` | UUID | Unique quote identifier |
| `customer_id` | UUID | Owning customer |
| `from_currency` | CHAR(3) | Source currency |
| `to_currency` | CHAR(3) | Destination currency |
| `from_amount` | Decimal | Amount customer sends |
| `to_amount` | Decimal | Amount customer receives (locked, 2dp) |
| `rate` | Decimal | Effective rate after spread (to_amount / from_amount) |
| `mid_rate` | Decimal | Market mid-rate at quote time — combined with `rate` proves the exact spread applied; essential for audit and dispute resolution |
| `reference` | TEXT | Optional client-provided reference for reconciliation (e.g. `INV-2026-001`) |
| `expires_at` | TIMESTAMPTZ | 60 seconds from creation |
| `status` | enum | `pending`, `executed`, `expired`, `failed` |

**TTL:** 60 seconds. After expiry, execute returns HTTP 409.  
**Single-use:** Once executed, the status changes to `executed`; further execute calls return 409.  
**Rate lock:** `rate` and `to_amount` are immutable after quote creation. The live rate at execution time is irrelevant.

---

## Execute (Transaction)

### Preconditions (checked in order)
1. `Idempotency-Key` not seen before (if provided) → short-circuit return cached response
2. Quote exists and belongs to the requesting customer
3. `quote.status = 'pending'`
4. `quote.expires_at > now()`
5. `balance(customer, from_currency) >= quote.from_amount`

### Atomicity

All four writes occur inside one PostgreSQL transaction:
1. `UPDATE quotes SET status = 'executed'`
2. `UPDATE balances SET amount = amount - from_amount` (debit source)
3. `UPDATE balances SET amount = amount + to_amount` (credit destination)
4. `INSERT INTO transactions`

On any failure (precondition violated, DB error), the transaction rolls back and no state changes.

### Concurrency Safety

The quote row is locked with `SELECT ... FOR UPDATE` immediately after the idempotency check. Subsequent concurrent requests for the same quote wait on the lock, then see `status != 'pending'` and return 409. This guarantee holds across any number of API workers because it is enforced at the database layer, not the application layer.

### Deadlock Prevention

Balance rows are locked in ascending alphabetical order by currency code (`EUR` before `USD`, etc.). This total ordering eliminates the classic hold-and-wait deadlock between two concurrent transactions that touch the same two currencies in opposite order.

### Idempotency

- Client sends `Idempotency-Key: <uuid>` header.
- The key lookup runs inside the same transaction as the execute, before the `FOR UPDATE` lock. If the key is found, the cached transaction row is returned immediately with HTTP 200.
- A unique index on `transactions.idempotency_key` enforces uniqueness at the DB level as a safety net.
- Idempotency is scoped globally. Clients must use UUID-format keys.

---

## Rate Source

| Aspect | Policy |
|--------|--------|
| Primary source | `v6.exchangerate-api.com` — v6 API with provided key, USD base (`/v6/{key}/latest/USD`) |
| Fallback | Last successfully fetched rates |
| Staleness threshold | 10 minutes |
| Behaviour past threshold | New quote requests return HTTP 503; existing quotes may still be executed if they were generated before the threshold |
| Background refresh | Every 5 minutes |
| Manual refresh | `POST /rates/refresh` |
| On startup failure | Fallback seed rates used; warning logged |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | DB + rates health check |
| GET | `/metrics` | Prometheus-format metrics |
| POST | `/customers` | Create customer (accepts `phone`, `country`) |
| GET | `/customers` | List all customers |
| GET | `/customers/{id}` | Get customer |
| PATCH | `/customers/{id}/kyc` | Update KYC status (`pending`/`verified`/`rejected`) |
| GET | `/customers/{id}/balances` | List all currency balances |
| POST | `/customers/{id}/credit` | Internal fixture: credit a balance |
| POST | `/quotes` | Generate FX quote (accepts optional `reference`) |
| GET | `/quotes` | List all quotes (filter by `?customer_id=`) |
| GET | `/quotes/{id}` | Get single quote |
| POST | `/quotes/{id}/execute` | Execute quote atomically |
| GET | `/transactions` | List all transactions (filter by `?customer_id=`) |
| GET | `/transactions/{id}` | Get single transaction |
| GET | `/rates` | Current rates with buy/sell/mid for all 12 pairs |
| POST | `/rates/refresh` | Force rate refresh from upstream API |
| GET | `/docs` | Swagger UI — interactive testing (development mode only) |

---

## Error Semantics

| HTTP Code | `error_code` | Meaning |
|-----------|-------------|---------|
| 400 | `invalid_amount` / `unsupported_currency_pair` | Bad input |
| 404 | `quote_not_found` / `customer_not_found` | Resource missing |
| 409 | `quote_expired` / `quote_already_executed` | State conflict |
| 422 | `insufficient_balance` | Not enough funds |
| 503 | `rates_unavailable` | Rate source stale or unreachable |
| 500 | `internal_error` | Unexpected server error |

All error responses include `request_id` for log correlation.

---

## Observability

- **Structured JSON logs** (structlog) — every log record includes `request_id`, `method`, `path`, `status_code`, `duration_ms`.
- **X-Request-ID** — attached to every response; propagated from client header if provided.
- **Prometheus metrics** at `/metrics`: `fx_quotes_created_total`, `fx_quotes_executed_total`, `fx_quotes_expired_total`, `fx_quote_errors_total{error_code}`, `fx_rate_fetch_success_total`, `fx_rate_fetch_failure_total`, `fx_rates_stale`, `fx_http_request_duration_seconds` (histogram with P50/P95/P99 buckets).
- **`/healthz`** — returns `ok`, `degraded` (stale rates), or `unhealthy` (DB unreachable).

---

## Out of Scope

- Authentication and authorization
- Per-customer rate limits or tiered pricing
- Historical rate data or rate charts
- Webhooks or async execution notifications
- Partial fills or split execution
- Currency conversion fees beyond the spread
- Multi-region / geo-routing
