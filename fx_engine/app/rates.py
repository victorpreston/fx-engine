"""Rate provider — fetches live rates, applies spreads, handles staleness."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
import structlog

from app.config import settings
from app.exceptions import RatesUnavailableError, UnsupportedCurrencyPairError

log = structlog.get_logger(__name__)

# Per-pair spread as a decimal fraction.  Customer always receives slightly
# worse than mid — bank retains the spread as revenue.
PAIR_SPREADS: dict[str, Decimal] = {
    "USD/EUR": Decimal("0.003"),
    "USD/KES": Decimal("0.0075"),
    "USD/NGN": Decimal("0.010"),
    "EUR/USD": Decimal("0.003"),
    "EUR/KES": Decimal("0.0075"),
    "EUR/NGN": Decimal("0.010"),
    "KES/USD": Decimal("0.0075"),
    "KES/EUR": Decimal("0.0075"),
    "KES/NGN": Decimal("0.015"),
    "NGN/USD": Decimal("0.010"),
    "NGN/EUR": Decimal("0.010"),
    "NGN/KES": Decimal("0.015"),
}

DEFAULT_SPREAD = Decimal("0.005")

# Fallback mid-rates used when the live API is unreachable at startup.
_FALLBACK_MID: dict[str, Decimal] = {
    "EUR": Decimal("0.9200"),
    "KES": Decimal("129.50"),
    "NGN": Decimal("1480.00"),
}

SUPPORTED_CURRENCIES = {"USD", "EUR", "KES", "NGN"}


def _compute_all_mids(usd_rates: dict[str, Decimal]) -> dict[str, Decimal]:
    """Derive all 12 cross-pair mid-rates from three USD-base rates."""
    eur = usd_rates["EUR"]
    kes = usd_rates["KES"]
    ngn = usd_rates["NGN"]
    return {
        "USD/EUR": eur,
        "USD/KES": kes,
        "USD/NGN": ngn,
        "EUR/USD": Decimal("1") / eur,
        "EUR/KES": kes / eur,
        "EUR/NGN": ngn / eur,
        "KES/USD": Decimal("1") / kes,
        "KES/EUR": eur / kes,
        "KES/NGN": ngn / kes,
        "NGN/USD": Decimal("1") / ngn,
        "NGN/EUR": eur / ngn,
        "NGN/KES": kes / ngn,
    }


class RateProvider:
    """
    Thread-safe async rate provider.

    Mid-rates are fetched from exchangeratesapi.io (free tier).  On failure
    the provider serves the last known rates up to `rate_stale_seconds`; after
    that threshold new quote requests are rejected with 503.
    """

    def __init__(self) -> None:
        self._mids: dict[str, Decimal] = {}
        self._fetched_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)
        try:
            await self._do_refresh()
        except Exception as exc:
            log.warning("initial rate fetch failed, using fallback", error=str(exc))
            self._mids = _compute_all_mids(_FALLBACK_MID)
            self._fetched_at = datetime.now(timezone.utc)

        self._refresh_task = asyncio.create_task(self._background_loop())

    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
        if self._http:
            await self._http.aclose()

    # ── public API ───────────────────────────────────────────────────────────

    def is_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        age = (datetime.now(timezone.utc) - self._fetched_at).total_seconds()
        return age > settings.rate_stale_seconds

    def get_effective_rate(self, from_ccy: str, to_ccy: str) -> Decimal:
        """
        Return the all-in rate (to_amount / from_amount) for the customer.

        The rate is the mid-rate adjusted by the pair-specific spread so that
        the customer always receives slightly less than mid — the bank retains
        the difference as revenue.

        Raises RatesUnavailableError if rates are stale.
        Raises UnsupportedCurrencyPairError if the pair is not supported.
        """
        if self.is_stale():
            raise RatesUnavailableError(
                f"Exchange rates have not been refreshed in over "
                f"{settings.rate_stale_seconds}s. Retry shortly."
            )
        pair = f"{from_ccy}/{to_ccy}"
        mid = self._mids.get(pair)
        if mid is None:
            raise UnsupportedCurrencyPairError(from_ccy, to_ccy)

        spread = PAIR_SPREADS.get(pair, DEFAULT_SPREAD)
        # Customer receives mid × (1 − spread): always slightly worse than mid.
        return mid * (Decimal("1") - spread)

    def snapshot(self) -> dict:
        result: dict = {}
        for pair, mid in self._mids.items():
            spread = PAIR_SPREADS.get(pair, DEFAULT_SPREAD)
            buy = mid * (Decimal("1") - spread)
            sell = mid * (Decimal("1") + spread)
            result[pair] = {
                "mid": str(mid.normalize()),
                "buy": str(buy.normalize()),
                "sell": str(sell.normalize()),
                "spread_pct": str((spread * 100).normalize()),
            }
        return result

    def last_updated(self) -> datetime | None:
        return self._fetched_at

    async def refresh(self) -> None:
        """Force an immediate refresh. Called by POST /rates/refresh."""
        await self._do_refresh()

    # ── internals ────────────────────────────────────────────────────────────

    async def _background_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.rate_refresh_interval_seconds)
            try:
                await self._do_refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("background rate refresh failed", error=str(exc))

    async def _do_refresh(self) -> None:
        async with self._lock:
            assert self._http is not None
            resp = await self._http.get(f"{settings.rate_api_url}/USD")
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("rates", data)

            usd_rates = {
                "EUR": Decimal(str(raw["EUR"])),
                "KES": Decimal(str(raw["KES"])),
                "NGN": Decimal(str(raw["NGN"])),
            }
            self._mids = _compute_all_mids(usd_rates)
            self._fetched_at = datetime.now(timezone.utc)
            log.info("rates refreshed", pairs=len(self._mids), source="api")


# Module-level singleton — shared across the process.
rate_provider = RateProvider()
