# Skill: Swap Rate Source Provider

You are working on the FX Engine. The task is to replace the current rate source (`api.exchangerate-api.com`) with a new provider.

## What to change

### `fx_engine/app/rates.py` — `_do_refresh()`
The current implementation:
```python
resp = await self._http.get(f"{settings.rate_api_url}/USD")
resp.raise_for_status()
data = resp.json()
raw = data.get("rates", data)
usd_rates = {
    "EUR": Decimal(str(raw["EUR"])),
    "KES": Decimal(str(raw["KES"])),
    "NGN": Decimal(str(raw["NGN"])),
}
self._mids = _compute_all_mids(usd_rates)
```

Adapt this to the new provider's response shape. The output must always produce a dict with keys `"EUR"`, `"KES"`, `"NGN"` as `Decimal` values representing the rate of each currency per 1 USD.

### `fx_engine/app/config.py`
Update `rate_api_url` default if the base URL changes.

### `fx_engine/.env.example`
Update `RATE_API_URL` to the new provider's base URL.

## What NOT to change
- `_compute_all_mids()` — this derives all 12 pairs and must not change
- `PAIR_SPREADS` — spread policy is independent of the source
- Staleness detection — `is_stale()` logic stays the same
- The fallback `_FALLBACK_MID` values — update only if the new source gives significantly different rates

## Tests to run after
```bash
pytest tests/test_rates.py -v
```
The `test_refresh_updates_rates_and_timestamp` test mocks the HTTP client — update the mock response shape to match the new provider if needed.
