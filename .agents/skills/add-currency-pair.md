# Skill: Add Currency Pair

You are working on the FX Engine — a FastAPI + PostgreSQL foreign exchange API.

## Your task
Add support for a new currency to the engine. You will be told the currency code (e.g. `GBP`).

## What to change

### 1. `fx_engine/app/rates.py`
- Add the new currency to `SUPPORTED_CURRENCIES`
- Add per-pair spread entries to `PAIR_SPREADS` for every `NEW/existing` and `existing/NEW` combination
- Verify `_compute_all_mids()` will derive the new pairs correctly from the USD base rate
- Add a fallback value to `_FALLBACK_MID` for `USD/NEW`

### 2. `fx_engine/app/schemas.py`
- Add the new currency code to the `SUPPORTED_CURRENCIES` set used in validators

### 3. `fx_engine/migrations/`
- No schema changes needed — balances are currency-agnostic

### 4. `fx_engine/tests/test_quotes.py`
- Add the new pairs to `test_all_pairs_produce_positive_quote`

### 5. `fx_engine/tests/test_rates.py`
- Update `test_all_12_pairs_covered` — the name will need updating if pair count changes

## Constraints
- Never use `float`. All rates must be `Decimal`.
- The spread for exotic pairs should be wider than USD/EUR (0.3%). Use at least 1.0%.
- Run `pytest tests/test_rates.py tests/test_quotes.py` to verify before finishing.
