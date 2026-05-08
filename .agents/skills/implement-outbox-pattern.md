# Skill: Implement the Outbox Pattern for Reliable Event Delivery

You are working on the FX Engine. The task is to ensure that RabbitMQ events
(quote.created, quote.executed, quote.expired) are never silently dropped when
the broker is unavailable at publish time.

## Why this is needed

Currently events are fire-and-forget after the database transaction commits. If
RabbitMQ is down for 10 seconds, every execute during that window produces no
event — the downstream notification service, audit service, and ledger never
hear about it. The Outbox pattern solves this without distributed transactions.

## How it works

1. Inside the database transaction (before commit), write the event to an
   `outbox` table as a row.
2. After commit, try to publish immediately as before (fast path).
3. A separate background process polls the `outbox` table for undelivered rows
   and retries publishing them until they succeed, then marks them delivered.

## Migration

Create a new Alembic revision:

```python
def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS outbox (
            id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            event_type  TEXT        NOT NULL,
            payload     JSONB       NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            delivered_at TIMESTAMPTZ,
            attempts    INT         NOT NULL DEFAULT 0
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_outbox_undelivered
            ON outbox(created_at)
            WHERE delivered_at IS NULL
    """)
```

## app/services/events.py — add outbox writer

```python
async def write_to_outbox(conn: asyncpg.Connection, event_type: str, payload: dict) -> None:
    """Call this INSIDE the database transaction before commit."""
    import json
    await conn.execute(
        "INSERT INTO outbox (event_type, payload) VALUES ($1, $2::jsonb)",
        event_type,
        json.dumps(payload),
    )
```

## app/engine/fx.py — use outbox inside the transaction

Replace the lazy `from app.services.events import publish; await publish(...)` calls
that happen after the transaction with `write_to_outbox(conn, ...)` calls inside
the transaction block, before the RETURNING fetch.

## Background relay worker

Create `app/workers/outbox_relay.py`:

```python
async def relay_loop(pool: asyncpg.Pool, publisher) -> None:
    while True:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, event_type, payload FROM outbox
                WHERE delivered_at IS NULL
                ORDER BY created_at
                LIMIT 50
                FOR UPDATE SKIP LOCKED
                """
            )
            for row in rows:
                try:
                    await publisher(row["event_type"], row["payload"])
                    await conn.execute(
                        "UPDATE outbox SET delivered_at = NOW() WHERE id = $1",
                        row["id"],
                    )
                except Exception:
                    await conn.execute(
                        "UPDATE outbox SET attempts = attempts + 1 WHERE id = $1",
                        row["id"],
                    )
        await asyncio.sleep(5)
```

Start this as an `asyncio.create_task` in the FastAPI lifespan alongside `rate_provider.start()`.

## What NOT to change
- The `publish()` function in `events.py` — keep it for the immediate fast-path attempt
- The RabbitMQ exchange topology and routing keys
- The existing event payload structure
