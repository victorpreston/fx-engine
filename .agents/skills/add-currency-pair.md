# Skill: Add Currency Pair

You are working on the FX Engine — a FastAPI + PostgreSQL foreign exchange API.

## Your task
Add support for a new currency. You will be told the currency code (e.g. `GBP`).

## What to change

### 1. `fx_engine/app/providers/rates.py`
- Add the new currency to `SUPPORTED_CURRENCIES`
- Add per-pair spread entries to `PAIR_SPREADS` for every `NEW/existing` and `existing/NEW` combination (12 new entries if adding one currency to a 4-currency system)
- Update `_compute_all_mids()` to derive the new pairs from USD-base rates
- Add a fallback value to `_FALLBACK_MID` for `USD/NEW`

### 2. `fx_engine/app/models/shared.py`
- Add the new currency code to `SUPPORTED_CURRENCIES`

### 3. `fx_engine/app/models/customer.py` and `models/quote.py`
- Both import `SUPPORTED_CURRENCIES` from `models/shared.py` — the validator updates automatically.

### 4. `fx_engine/db/versions/` — new Alembic migration (optional)
- If adding a column or constraint related to the new currency, create a new migration.
- No schema changes are needed just to add a currency to the rate table — rates are in-memory.

### 5. `fx_engine/tests/test_quotes.py`
- Add the new pairs to `test_all_pairs_produce_positive_quote`

### 6. `fx_engine/tests/test_rates.py`
- Update `test_all_12_pairs_covered` — rename the assertion if the pair count changes

## Constraints
- Never use `float`. All rates must be `Decimal`.
- The spread for exotic pairs should be wider than USD/EUR (0.3%). Use at least 1.0%.
- Run `alembic upgrade head` if any schema changes were made.
- Run `pytest tests/test_rates.py tests/test_quotes.py` to verify before finishing.
