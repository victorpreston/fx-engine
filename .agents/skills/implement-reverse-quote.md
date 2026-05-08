# Skill: Implement Reverse Quote (to_amount → from_amount)

You are working on the FX Engine. The task is to support a reverse quote:
"how much USD do I need to send to receive exactly 50,000 KES?"

Currently the API only accepts `from_amount`. A reverse quote accepts
`to_amount` and back-calculates the required `from_amount`.

## The math

Standard quote:  `to_amount = from_amount × rate`
Reverse quote:   `from_amount = to_amount / rate`

Where `rate = mid × (1 − spread)`. So:

```
from_amount = to_amount / (mid × (1 − spread))
```

Round `from_amount` UP (ROUND_CEILING) — always charge slightly more than
the minimum so the engine never delivers less than the promised `to_amount`.

## app/models/quote.py — add reverse quote request

```python
class ReverseQuoteRequest(BaseModel):
    customer_id: UUID
    from_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    to_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    to_amount: Decimal = Field(..., gt=0, description="Exact amount the recipient should receive")
    reference: Optional[str] = Field(default=None, max_length=255)

    @field_validator("from_currency", "to_currency")
    @classmethod
    def currency_must_be_supported(cls, v: str) -> str:
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported currency: {v}")
        return v
```

## app/engine/fx.py — add reverse_generate_quote()

```python
async def reverse_generate_quote(
    conn: asyncpg.Connection,
    rate_provider: RateProvider,
    customer_id: str,
    from_currency: str,
    to_currency: str,
    to_amount: Decimal,
    reference: str | None = None,
) -> asyncpg.Record:
    rate = rate_provider.get_effective_rate(from_currency, to_currency)
    # Round UP so the engine always delivers at least to_amount
    from_amount = (to_amount / rate).quantize(QUANTUM, rounding=ROUND_CEILING)
    # Delegate to the standard generate_quote with the computed from_amount
    return await generate_quote(
        conn, rate_provider, customer_id,
        from_currency, to_currency, from_amount, reference,
    )
```

## app/routes/quotes.py — add endpoint

```python
@router.post("/reverse", response_model=QuoteResponse, status_code=201)
async def create_reverse_quote(
    body: ReverseQuoteRequest,
    conn: asyncpg.Connection = Depends(get_connection),
):
    row = await fx.reverse_generate_quote(
        conn=conn,
        rate_provider=rate_provider,
        customer_id=str(body.customer_id),
        from_currency=body.from_currency,
        to_currency=body.to_currency,
        to_amount=body.to_amount,
        reference=body.reference,
    )
    return {...}  # same shape as create_quote response
```

## Invariant to test

```python
def test_reverse_quote_delivers_at_least_requested_amount():
    # Execute a reverse quote for to_amount=50000 KES
    # After execute, customer KES balance must be >= 50000
    # (ROUND_CEILING on from_amount guarantees this)
```

## What NOT to change
- The execute path — reverse quotes produce standard quote rows and execute identically
- The database schema — no new columns needed
- The spread model — same effective rate used in both directions
