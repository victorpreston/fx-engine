"""Tests for the rate provider — staleness, fallback, refresh."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.exceptions import RatesUnavailableError, UnsupportedCurrencyPairError
from app.providers.rates import _FALLBACK_MID, RateProvider, _compute_all_mids


@pytest.fixture
def provider() -> RateProvider:
    p = RateProvider()
    mids = _compute_all_mids(_FALLBACK_MID)
    p._mids = mids
    p._fetched_at = datetime.now(timezone.utc)
    return p


def test_get_effective_rate_returns_positive(provider):
    rate = provider.get_effective_rate("USD", "KES")
    assert rate > 0


def test_effective_rate_is_worse_than_mid(provider):
    """Customer-facing rate must always be below the mid-rate."""
    mid = provider._mids["USD/KES"]
    rate = provider.get_effective_rate("USD", "KES")
    assert rate < mid


def test_stale_rates_raise_unavailable(provider):
    from app.config import settings

    provider._fetched_at = datetime.now(timezone.utc) - timedelta(
        seconds=settings.rate_stale_seconds + 60
    )
    with pytest.raises(RatesUnavailableError):
        provider.get_effective_rate("USD", "KES")


def test_none_fetched_at_raises_unavailable(provider):
    provider._fetched_at = None
    with pytest.raises(RatesUnavailableError):
        provider.get_effective_rate("USD", "EUR")


def test_unsupported_pair_raises(provider):
    with pytest.raises(UnsupportedCurrencyPairError):
        provider.get_effective_rate("USD", "GBP")


def test_snapshot_contains_all_pairs(provider):
    snap = provider.snapshot()
    assert len(snap) == 12
    for pair in snap:
        assert "mid" in snap[pair]
        assert "buy" in snap[pair]
        assert "sell" in snap[pair]
        assert "spread_pct" in snap[pair]


def test_snapshot_sell_gt_mid_gt_buy(provider):
    """sell > mid > buy for every pair — bank profits on both sides."""
    snap = provider.snapshot()
    for pair, vals in snap.items():
        mid = Decimal(vals["mid"])
        buy = Decimal(vals["buy"])
        sell = Decimal(vals["sell"])
        assert buy < mid < sell, (
            f"Pair {pair}: buy={buy}, mid={mid}, sell={sell} — spread direction wrong"
        )


async def test_refresh_updates_rates_and_timestamp(provider):
    old_time = provider._fetched_at
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "rates": {"EUR": 0.91, "KES": 130.5, "NGN": 1490.0}
    }
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    provider._http = mock_client

    await provider._do_refresh()

    assert provider._fetched_at > old_time
    assert provider._mids["USD/KES"] == Decimal("130.5")
    assert not provider.is_stale()


async def test_refresh_failure_preserves_existing_rates(provider):
    old_mids = dict(provider._mids)
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("network error")
    provider._http = mock_client

    with pytest.raises(Exception, match="network error"):
        await provider._do_refresh()

    # Rates must be unchanged.
    assert provider._mids == old_mids


def test_all_12_pairs_covered():
    from app.providers.rates import SUPPORTED_CURRENCIES

    currencies = list(SUPPORTED_CURRENCIES)
    mids = _compute_all_mids(_FALLBACK_MID)
    for a in currencies:
        for b in currencies:
            if a != b:
                assert f"{a}/{b}" in mids, f"Missing pair {a}/{b}"
