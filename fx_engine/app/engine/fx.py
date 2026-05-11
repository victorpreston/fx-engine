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

Ledger model: every money-moving operation writes to ledger_entries
(append-only) in the same transaction as the balance UPDATE.  balances is
a materialized cache; ledger_entries is the source of truth.  Invariant:
balance == SUM(credits) - SUM(debits) per (customer_id, currency).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import asyncpg
import structlog

from app.config import settings
from app.exceptions import (
    CustomerNotFoundError,
    ExecutionInProgressError,
    IdempotencyConflictError,
    IdempotencyKeyRequiredError,
    InsufficientBalanceError,
    QuoteAlreadyExecutedError,
    QuoteExpiredError,
    QuoteNotFoundError,
)
from app.providers.rates import RateProvider
from app.services.metrics import quotes_created, quotes_executed, quotes_expired

log = structlog.get_logger(__name__)

QUANTUM = Decimal("0.01")
_EXECUTE_ENDPOINT = "POST /quotes/{quote_id}/execute"


def _after_debit_hook() -> None:
    """No-op in production. Tests monkeypatch this to inject a failure
    between the debit UPDATE and the credit UPDATE, proving that the
    entire transaction rolls back atomically on error.
    """
    return None


def _request_hash(quote_id: str, customer_id: str) -> str:
    """Bind the idempotency key to the canonical execute request payload."""
    payload = json.dumps(
        {
            "endpoint": _EXECUTE_ENDPOINT,
            "quote_id": quote_id,
            "customer_id": customer_id,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


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

    Idempotency: Idempotency-Key is required.  The key is bound to a
    SHA-256 hash of the request payload.  Same key + same hash returns
    the cached transaction row.  Same key + different hash returns
    IdempotencyConflictError (409).

    Ledger: every balance mutation writes a paired debit/credit row to
    ledger_entries in the same transaction.  _after_debit_hook() is a
    no-op in production; tests override it to prove rollback atomicity.
    """
    if not idempotency_key:
        raise IdempotencyKeyRequiredError()

    req_hash = _request_hash(quote_id, customer_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ── 1. Idempotency: check for existing key inside transaction ──
            existing_idem = await conn.fetchrow(
                "SELECT * FROM idempotency_keys WHERE endpoint = $1 AND key = $2",
                _EXECUTE_ENDPOINT,
                idempotency_key,
            )
            if existing_idem is not None:
                if existing_idem["request_hash"] != req_hash:
                    raise IdempotencyConflictError(idempotency_key)
                if existing_idem["completed_at"] is not None:
                    # Completed replay — return stored transaction row.
                    log.info("idempotent_replay", idempotency_key=idempotency_key)
                    tx = await conn.fetchrow(
                        "SELECT * FROM transactions WHERE idempotency_key = $1",
                        idempotency_key,
                    )
                    return tx
                # completed_at is NULL: a previous call with this key is still
                # in-flight or failed mid-execute before completing.  Tell the
                # client to retry rather than racing to execute a second time.
                raise ExecutionInProgressError(idempotency_key)

            # Reserve the idempotency slot (in-flight guard).
            await conn.execute(
                """
                INSERT INTO idempotency_keys (endpoint, key, request_hash)
                VALUES ($1, $2, $3)
                ON CONFLICT (endpoint, key) DO NOTHING
                """,
                _EXECUTE_ENDPOINT,
                idempotency_key,
                req_hash,
            )

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

            # ── 5. Mark quote executed ────────────────────────────────────
            await conn.execute(
                "UPDATE quotes SET status = 'executed' WHERE id = $1",
                quote_id,
            )

            # ── 6. Record transaction ─────────────────────────────────────
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

            # ── 7. Debit source balance + ledger entry ────────────────────
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
                INSERT INTO ledger_entries
                    (customer_id, currency, amount, direction, reference_type, reference_id)
                VALUES ($1, $2, $3, 'debit', 'execution', $4)
                """,
                customer_id,
                from_ccy,
                str(from_amount),
                str(tx["id"]),
            )

            # Test hook — overridden by monkeypatch in atomicity tests to prove
            # that a failure here rolls back the entire transaction.
            _after_debit_hook()

            # ── 8. Credit destination balance + ledger entry ──────────────
            await conn.execute(
                """
                UPDATE balances SET amount = amount + $1, updated_at = NOW()
                WHERE customer_id = $2 AND currency = $3
                """,
                str(to_amount),
                customer_id,
                to_ccy,
            )
            await conn.execute(
                """
                INSERT INTO ledger_entries
                    (customer_id, currency, amount, direction, reference_type, reference_id)
                VALUES ($1, $2, $3, 'credit', 'execution', $4)
                """,
                customer_id,
                to_ccy,
                str(to_amount),
                str(tx["id"]),
            )

            # ── 9. Mark idempotency key completed + store response snapshot ──
            stored_payload = json.dumps(
                {
                    "id": str(tx["id"]),
                    "quote_id": str(tx["quote_id"]),
                    "customer_id": str(tx["customer_id"]),
                    "from_currency": tx["from_currency"],
                    "to_currency": tx["to_currency"],
                    "from_amount": str(Decimal(str(tx["from_amount"]))),
                    "to_amount": str(Decimal(str(tx["to_amount"]))),
                    "rate": str(Decimal(str(tx["rate"]))),
                    "status": tx["status"],
                    "executed_at": tx["executed_at"].isoformat(),
                }
            )
            await conn.execute(
                """
                UPDATE idempotency_keys
                   SET completed_at = NOW(), status_code = 200,
                       response_payload = $3::jsonb
                 WHERE endpoint = $1 AND key = $2
                """,
                _EXECUTE_ENDPOINT,
                idempotency_key,
                stored_payload,
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
