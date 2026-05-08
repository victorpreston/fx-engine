# Skill: Add an Async Background Worker

You are working on the FX Engine. The task is to add a recurring async task
that runs inside the FastAPI process — for example, a quote-expiry sweep that
proactively marks stale quotes rather than waiting for a client to attempt
execution.

## Pattern

Background workers are `asyncio.Task` objects started in the FastAPI lifespan
and cancelled on shutdown. Follow the same pattern as `RateProvider._background_loop`.

## Example: Quote Expiry Worker

### `app/workers/quote_expiry.py`

```python
from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)
_task: asyncio.Task | None = None


async def _sweep(pool: asyncpg.Pool) -> None:
    """Mark all pending quotes whose TTL has elapsed."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE quotes
               SET status = 'expired'
             WHERE status = 'pending'
               AND expires_at < NOW()
            """
        )
        count = int(result.split()[-1])
        if count:
            log.info("quote_expiry_sweep", expired=count)


async def _loop(pool: asyncpg.Pool, interval_seconds: int = 30) -> None:
    while True:
        try:
            await _sweep(pool)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("quote_expiry_sweep_failed", error=str(exc))
        await asyncio.sleep(interval_seconds)


async def start(pool: asyncpg.Pool) -> None:
    global _task
    _task = asyncio.create_task(_loop(pool))
    log.info("quote_expiry_worker_started")


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
```

### `app/main.py` — wire into lifespan

```python
from app.workers import quote_expiry

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    await quote_expiry.start(pool)   # ← add
    await rate_provider.start()
    ...
    yield
    ...
    await quote_expiry.stop()         # ← add
    await rate_provider.stop()
```

## Rules for all background workers

- Always handle `asyncio.CancelledError` by re-raising — this is how the lifespan
  shutdown propagates.
- Log failures with `log.warning`, never `log.error` unless the failure is unrecoverable.
  A transient DB error should not alert oncall.
- Use `FOR UPDATE SKIP LOCKED` if multiple workers could run the same sweep
  (e.g. in a multi-worker deployment). The quote expiry sweep uses `UPDATE` which
  is already atomic per-row, so no extra locking is needed.
- Keep sweep interval configurable via `settings` so it can be tuned in production
  without a code change.
- Add a Prometheus counter for rows processed per sweep so the worker is visible
  in Grafana.
