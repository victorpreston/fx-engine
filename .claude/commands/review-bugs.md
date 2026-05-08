# Review Planted Bugs

Analyses the `planted_bugs/` directory for issues. Use this to cross-check findings against `REVIEW.md`.

## Run the existing tests to confirm they still pass
```bash
cd planted_bugs && pip install -r requirements.txt -q && pytest -v
```

## Start the buggy server for manual testing
```bash
cd planted_bugs && python app.py
```

## Key areas to inspect
- `fx.py:60-61` — float arithmetic in `generate_quote` (Bug 6)
- `fx.py:112-138` — race condition: lock acquired after expiry check (Bug 1)
- `fx.py:126-132` — rate re-fetched at execute time instead of using stored rate (Bug 2)
- `fx.py:188-190` — inverse rate discards spread (Bug 7)
- `fx.py:193-200` — cross-pair routing returns wildly wrong rate (Bug 8)
- `db.py:12-19` — no explicit rollback in context manager (Bug 9)

All 10 findings (3 blockers, 5 major, 1 minor, 1 nit) are documented in `REVIEW.md`.
