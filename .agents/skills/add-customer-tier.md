# Skill: Add Per-Customer Spread Tiers

You are working on the FX Engine. The task is to give premium customers tighter
spreads than standard customers, implemented as a `tier` column on the customer
table that flows through to the rate calculation.

## Migration

Create a new Alembic revision:

```python
def upgrade() -> None:
    op.execute("""
        ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'standard'
                CONSTRAINT customers_tier_check
                    CHECK (tier IN ('standard', 'premium'))
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_customers_tier
            ON customers(tier)
            WHERE tier = 'premium'
    """)
```

## Spread configuration

In `app/providers/rates.py`, add tier-based spread overrides:

```python
PREMIUM_SPREAD_DISCOUNT = Decimal("0.50")  # premium pays 50% of standard spread

def get_spread(pair: str, tier: str = "standard") -> Decimal:
    base = PAIR_SPREADS.get(pair, DEFAULT_SPREAD)
    if tier == "premium":
        return (base * PREMIUM_SPREAD_DISCOUNT).quantize(Decimal("0.0001"))
    return base
```

## RateProvider — expose tier-aware rate

```python
def get_effective_rate(self, from_ccy: str, to_ccy: str, tier: str = "standard") -> Decimal:
    ...
    spread = get_spread(pair, tier)
    return mid * (Decimal("1") - spread)
```

## app/engine/fx.py — pass tier to rate lookup

In `generate_quote()`, fetch the customer's tier alongside the existence check:

```python
customer = await conn.fetchrow(
    "SELECT id, tier FROM customers WHERE id = $1", customer_id
)
tier = customer["tier"] if customer else "standard"
rate = rate_provider.get_effective_rate(from_currency, to_currency, tier=tier)
```

## app/models/customer.py — add tier to schemas

```python
class CustomerCreate(BaseModel):
    ...
    tier: Literal["standard", "premium"] = "standard"

class CustomerResponse(BaseModel):
    ...
    tier: str = "standard"
```

## app/routes/customers.py — pass tier on INSERT

```python
row = await conn.fetchrow(
    "INSERT INTO customers (name, email, phone, country, tier) VALUES ($1,$2,$3,$4,$5) RETURNING *",
    body.name, body.email, body.phone, body.country, body.tier,
)
```

## Tests to add

- `test_premium_customer_gets_tighter_spread` — create premium customer, generate quote, assert `rate > mid * (1 - premium_spread)`
- `test_standard_customer_gets_standard_spread` — same for standard tier
- `test_same_amount_different_tiers_different_to_amount` — both customers, same quote params, assert premium `to_amount > standard to_amount`
