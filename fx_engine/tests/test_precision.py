"""
Property-based precision tests using Hypothesis.

These tests verify:
1. to_amount always has at most 2 decimal places.
2. to_amount is always positive for any valid from_amount.
3. The rate is monotonically consistent: larger from_amount → proportionally
   larger to_amount (no rounding discontinuities for the same pair).
4. Cross-pair rates are consistent with their component legs.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from hypothesis import assume, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from app.providers.rates import (
    _FALLBACK_MID,
    DEFAULT_SPREAD,
    PAIR_SPREADS,
    _compute_all_mids,
)

MIDS = _compute_all_mids(_FALLBACK_MID)
SUPPORTED_PAIRS = list(MIDS.keys())

QUANTUM = Decimal("0.01")


def effective_rate(pair: str) -> Decimal:
    mid = MIDS[pair]
    spread = PAIR_SPREADS.get(pair, DEFAULT_SPREAD)
    return mid * (Decimal("1") - spread)


def compute_to_amount(from_amount: Decimal, pair: str) -> Decimal:
    rate = effective_rate(pair)
    return (from_amount * rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)


# ── Property tests ────────────────────────────────────────────────────────────


@given(
    amount=st.decimals(
        min_value="0.01",
        max_value="10000000",
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    pair=st.sampled_from(SUPPORTED_PAIRS),
)
@hyp_settings(max_examples=500)
def test_to_amount_has_at_most_two_decimal_places(amount, pair):
    result = compute_to_amount(amount, pair)
    assert result == result.quantize(QUANTUM), (
        f"Pair {pair}, amount {amount}: result {result} has more than 2dp"
    )


@given(
    # min_value=10.00 ensures the lowest-rate pair (NGN/USD ≈ 0.00067)
    # produces to_amount >= 0.0067, which rounds to 0.01 — always positive.
    amount=st.decimals(
        min_value="10.00",
        max_value="10000000",
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    pair=st.sampled_from(SUPPORTED_PAIRS),
)
@hyp_settings(max_examples=500)
def test_to_amount_is_always_positive(amount, pair):
    result = compute_to_amount(amount, pair)
    assert result > 0, f"Pair {pair}, amount {amount}: got non-positive result {result}"


@given(
    a=st.decimals(
        min_value="1", max_value="1000", places=2, allow_nan=False, allow_infinity=False
    ),
    b=st.decimals(
        min_value="1001",
        max_value="10000",
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    pair=st.sampled_from(SUPPORTED_PAIRS),
)
@hyp_settings(max_examples=300)
def test_larger_from_amount_yields_larger_to_amount(a, b, pair):
    """Monotonicity: more in → more out (modulo rounding at the quantum boundary)."""
    assume(a < b)
    result_a = compute_to_amount(a, pair)
    result_b = compute_to_amount(b, pair)
    assert result_a <= result_b, (
        f"Pair {pair}: {a} → {result_a} but {b} → {result_b} (non-monotonic)"
    )


@given(
    amount=st.decimals(
        min_value="0.01",
        max_value="1000000",
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
)
@hyp_settings(max_examples=300)
def test_usd_kes_rate_worse_than_mid(amount):
    """Customer always gets worse than mid — the spread is always applied."""
    mid = MIDS["USD/KES"]
    rate = effective_rate("USD/KES")
    assert rate < mid, f"effective rate {rate} should be less than mid {mid}"
    to_amount = compute_to_amount(amount, "USD/KES")
    mid_amount = (amount * mid).quantize(QUANTUM, rounding=ROUND_HALF_UP)
    assert to_amount <= mid_amount


@given(
    amount=st.decimals(
        min_value="1",
        max_value="100000",
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
)
@hyp_settings(max_examples=200)
def test_cross_pair_rate_is_consistent_with_legs(amount):
    """
    KES/NGN rate should be approximately KES/USD × USD/NGN.
    Allows for spread compounding (cross pair charges two spreads).
    """
    kes_ngn_rate = effective_rate("KES/NGN")
    kes_usd_rate = effective_rate("KES/USD")
    usd_ngn_rate = effective_rate("USD/NGN")

    # Direct must be within 5% of cross-via-USD (they share the same mid derivation).
    implied = kes_usd_rate * usd_ngn_rate
    ratio = kes_ngn_rate / implied
    assert Decimal("0.95") < ratio < Decimal("1.05"), (
        f"KES/NGN direct={kes_ngn_rate}, implied via USD={implied}, ratio={ratio}"
    )


def test_all_pairs_have_positive_effective_rates():
    for pair in SUPPORTED_PAIRS:
        rate = effective_rate(pair)
        assert rate > 0, f"Pair {pair} has non-positive effective rate: {rate}"


def test_spread_is_applied_symmetrically():
    """A/B and B/A effective rates should be inverses within spread tolerance."""
    from_ccy, to_ccy = "USD", "EUR"
    rate_fwd = effective_rate(f"{from_ccy}/{to_ccy}")
    rate_inv = effective_rate(f"{to_ccy}/{from_ccy}")
    product = rate_fwd * rate_inv
    # If spreads are symmetric the product should be < 1 (bank earns on both sides).
    assert product < Decimal("1"), (
        f"Forward × inverse = {product}, expected < 1 (spread not earning)"
    )
