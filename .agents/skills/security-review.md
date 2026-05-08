# Skill: Security Review — FX Engine Changes

You are reviewing changes to the FX Engine, a financial API handling real currency conversions.

## What to check

### Financial correctness (highest priority)
- Is `float` used anywhere in rate or amount calculations? It must never be. All arithmetic uses `Decimal`.
- Does any new code re-fetch rates at execute time? The rate must be locked at quote generation.
- Are balance updates inside the same transaction as quote status updates? They must be atomic.
- Does any new balance update skip the `CHECK (amount >= 0)` constraint path?

### Concurrency safety
- Does any new code use `threading.Lock()` or `asyncio.Lock()` for financial state? Should use `SELECT FOR UPDATE`.
- Is the idempotency key check inside the transaction? Moving it outside creates a TOCTOU race.
- Are balance rows locked in alphabetical currency order? Different order = deadlock risk.

### Input validation
- Are all currency inputs validated against `SUPPORTED_CURRENCIES`?
- Are amounts validated as `> 0` and typed as `Decimal` (not `float` or `int`)?
- Are UUIDs validated by Pydantic before hitting the database?

### Data exposure
- Does any error response reveal whether a quote/customer exists to an unauthorized caller? (Quote ownership check should return 404, not 403.)
- Are customer IDs from the request body verified against the quote's stored `customer_id`?

### SQL safety
- Are all query parameters passed as positional arguments (`$1`, `$2`)? No string interpolation in SQL.
- Does any new raw SQL bypass asyncpg's parameterization?

## Output format
For each issue found:
- **Severity**: blocker / major / minor / nit
- **Location**: file:line
- **What's wrong** and **production impact**
- **How to fix**
