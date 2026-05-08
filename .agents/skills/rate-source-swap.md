# Skill: Swap Rate Source Provider

You are working on the FX Engine. The task is to replace the current rate source
(`v6.exchangerate-api.com`) with a new provider.

## Current implementation

```python
# app/providers/rates.py — _do_refresh()
url = f"{settings.rate_api_url}/{settings.rate_api_key}/latest/USD"
resp = await self._http.get(url)
resp.raise_for_status()
data = resp.json()
# v6 uses "conversion_rates"; fallback handles test mocks
raw = data.get("conversion_rates", data.get("rates", data))
usd_rates = {
    "EUR": Decimal(str(raw["EUR"])),
    "KES": Decimal(str(raw["KES"])),
    "NGN": Decimal(str(raw["NGN"])),
}
self._mids = _compute_all_mids(usd_rates)
```

## What to change for a new provider

Adapt `_do_refresh()` to match the new provider's response shape. The output
must always produce a dict with keys `"EUR"`, `"KES"`, `"NGN"` as `Decimal`
values representing the rate of each currency per 1 USD.

### `fx_engine/app/config.py`
Update `rate_api_url` and `rate_api_key` defaults (or remove `rate_api_key` if the
new provider uses a different auth mechanism, e.g. a header).

### `fx_engine/.env.example` and `fx_engine/.env`
Update `RATE_API_URL` and `RATE_API_KEY` for the new provider.

### `fx_engine/docker-compose.yml`
Update the `RATE_API_URL` and `RATE_API_KEY` environment variables in the `api` service.

## What NOT to change
- `_compute_all_mids()` — derives all 12 pairs from three USD-base rates; provider-agnostic
- `PAIR_SPREADS` — spread policy is independent of the data source
- Staleness detection — `is_stale()` logic is provider-agnostic
- `_FALLBACK_MID` values — update only if the new source gives significantly different rates
- Redis caching — `cache_rates()` is called at the end of `_do_refresh()` regardless of source

## Tests to run after
```bash
pytest tests/test_rates.py -v
```

Update the mock in `test_refresh_updates_rates_and_timestamp` to match the new
provider's response key (currently `"conversion_rates"`).
