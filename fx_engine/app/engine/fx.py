"""
FX engine core — quote generation and atomic two-leg execution.

Concurrency safety is implemented at the database layer via PostgreSQL
SELECT ... FOR UPDATE, not via application-level locks.  This means the
guarantee holds across multiple API workers and process restarts.

Rounding: all intermediate arithmetic uses Python Decimal with full
precision; the final to_amount is quantized to 2 decimal places
(ROUND_HALF_UP) at quote generation time.  The locked-in to_amount is
stored in the quotes table and reused at execution time — the rate is
never re-fetched on execute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import asyncpg
import structlog

from app.config import settings
from app.exceptions import (
    CustomerNotFoundError,
    InsufficientBalanceError,
    QuoteAlreadyExecutedError,
    QuoteExpiredError,
    QuoteNotFoundError,
)
from app.providers.rates import RateProvider
from app.services.metrics import quotes_created, quotes_executed, quotes_expired

log = structlog.get_logger(__name__)

QUANTUM = Decimal("0.01")


async def generate_quote(
    conn: asyncpg.Connection,
    rate_provider: RateProvider,
    customer_id: str,
    from_currency: str,
    to_currency: str,
    from_amount: Decimal,
    reference: str | None = None,
) -> asyncpg.Record:
    """
    Create and persist an FX quote.

    The effective rate is locked in at quote time.  Execution will use
    exactly this rate — it will not be re-fetched.
    """
    if from_currency == to_currency:
        raise ValueError("from_currency and to_currency must differ")
    if from_amount <= 0:
        raise ValueError("amount must be positive")

    customer = await conn.fetchrow(
        "SELECT id FROM customers WHERE id = $1", customer_id
    )
    if customer is None:
        raise CustomerNotFoundError(customer_id)

    rate = rate_provider.get_effective_rate(from_currency, to_currency)
    mid_rate = rate_provider.get_mid_rate(from_currency, to_currency)
    to_amount = (from_amount * rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.quote_ttl_seconds)

    row = await conn.fetchrow(
        """
        INSERT INTO quotes
            (id, customer_id, from_currency, to_currency,
             from_amount, to_amount, rate, mid_rate, reference, expires_at)
        VALUES
            (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING *
        """,
        customer_id,
        from_currency,
        to_currency,
        str(from_amount),
        str(to_amount),
        str(rate),
        str(mid_rate) if mid_rate is not None else None,
        reference,
        expires_at,
    )
    log.info(
        "quote_created",
        quote_id=str(row["id"]),
        customer_id=customer_id,
        pair=f"{from_currency}/{to_currency}",
        from_amount=str(from_amount),
        to_amount=str(to_amount),
        rate=str(rate),
    )
    quotes_created.inc()
    from app.services.events import publish

    await publish(
        "quote.created",
        {
            "quote_id": str(row["id"]),
            "customer_id": customer_id,
            "from_currency": from_currency,
            "to_currency": to_currency,
            "from_amount": str(from_amount),
            "to_amount": str(to_amount),
            "rate": str(rate),
            "expires_at": row["expires_at"].isoformat(),
        },
    )
    return row


async def execute_quote(
    pool: asyncpg.Pool,
    customer_id: str,
    quote_id: str,
    idempotency_key: Optional[str] = None,
) -> asyncpg.Record:
    """
    Execute a quote atomically: debit from_currency, credit to_currency.

    All reads, checks, and writes occur inside a single serialisable
    transaction.  The quote row is locked with FOR UPDATE before the
    status check, guaranteeing that exactly one concurrent caller
    succeeds even under heavy parallel load.

    Deadlock prevention: balance rows are always locked in ascending
    alphabetical order by currency code.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # ── 1. Idempotency: return cached response if key was seen ──────
            if idempotency_key:
                existing = await conn.fetchrow(
                    "SELECT * FROM transactions WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing is not None:
                    log.info(
                        "idempotent_replay",
                        idempotency_key=idempotency_key,
                        transaction_id=str(existing["id"]),
                    )
                    return existing

            # ── 2. Lock the quote row (SELECT FOR UPDATE) ─────────────────
            quote = await conn.fetchrow(
                "SELECT * FROM quotes WHERE id = $1 FOR UPDATE",
                quote_id,
            )
            if quote is None:
                raise QuoteNotFoundError(quote_id)

            if str(quote["customer_id"]) != str(customer_id):
                raise QuoteNotFoundError(quote_id)

            now = datetime.now(timezone.utc)

            if quote["expires_at"].astimezone(timezone.utc) < now:
                await conn.execute(
                    "UPDATE quotes SET status = 'expired' WHERE id = $1",
                    quote_id,
                )
                quotes_expired.inc()
                from app.services.events import publish

                await publish("quote.expired", {"quote_id": quote_id})
                raise QuoteExpiredError(quote_id)

            if quote["status"] != "pending":
                raise QuoteAlreadyExecutedError(quote_id)

            from_ccy = quote["from_currency"]
            to_ccy = quote["to_currency"]
            from_amount = Decimal(str(quote["from_amount"]))
            to_amount = Decimal(str(quote["to_amount"]))
            rate = Decimal(str(quote["rate"]))

            # ── 3. Upsert balance rows, then lock them in stable order ─────
            currencies_ordered = sorted([from_ccy, to_ccy])
            for ccy in currencies_ordered:
                await conn.execute(
                    """
                    INSERT INTO balances (customer_id, currency, amount)
                    VALUES ($1, $2, 0)
                    ON CONFLICT (customer_id, currency) DO NOTHING
                    """,
                    customer_id,
                    ccy,
                )

            balance_rows = await conn.fetch(
                """
                SELECT currency, amount FROM balances
                WHERE customer_id = $1 AND currency = ANY($2::text[])
                ORDER BY currency
                FOR UPDATE
                """,
                customer_id,
                currencies_ordered,
            )
            balances = {r["currency"]: Decimal(str(r["amount"])) for r in balance_rows}

            # ── 4. Check sufficient balance ────────────────────────────────
            available = balances.get(from_ccy, Decimal("0"))
            if available < from_amount:
                raise InsufficientBalanceError(from_ccy, from_amount, available)

            # ── 5. Execute: debit source, credit destination, mark quote ──
            await conn.execute(
                "UPDATE quotes SET status = 'executed' WHERE id = $1",
                quote_id,
            )
            await conn.execute(
                """
                UPDATE balances SET amount = amount - $1, updated_at = NOW()
                WHERE customer_id = $2 AND currency = $3
                """,
                str(from_amount),
                customer_id,
                from_ccy,
            )
            await conn.execute(
                """
                UPDATE balances SET amount = amount + $1, updated_at = NOW()
                WHERE customer_id = $2 AND currency = $3
                """,
                str(to_amount),
                customer_id,
                to_ccy,
            )

            # ── 6. Record transaction (idempotency key stored here) ────────
            tx = await conn.fetchrow(
                """
                INSERT INTO transactions
                    (id, quote_id, customer_id, idempotency_key,
                     from_currency, to_currency, from_amount, to_amount,
                     rate, status)
                VALUES
                    (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, 'success')
                RETURNING *
                """,
                quote_id,
                customer_id,
                idempotency_key,
                from_ccy,
                to_ccy,
                str(from_amount),
                str(to_amount),
                str(rate),
            )

        log.info(
            "quote_executed",
            transaction_id=str(tx["id"]),
            quote_id=quote_id,
            customer_id=customer_id,
            pair=f"{from_ccy}/{to_ccy}",
            from_amount=str(from_amount),
            to_amount=str(to_amount),
        )
        quotes_executed.inc()
        from app.services.events import publish

        await publish(
            "quote.executed",
            {
                "transaction_id": str(tx["id"]),
                "quote_id": quote_id,
                "customer_id": customer_id,
                "from_currency": from_ccy,
                "to_currency": to_ccy,
                "from_amount": str(from_amount),
                "to_amount": str(to_amount),
                "rate": str(rate),
            },
        )
        return tx
