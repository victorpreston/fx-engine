from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
)

from app.database import get_pool
from app.rates import rate_provider
from app.schemas import HealthComponent, HealthResponse

router = APIRouter(tags=["observability"])

# ── Prometheus metrics ────────────────────────────────────────────────────────
quotes_created = Counter("fx_quotes_created_total", "Total FX quotes generated")
quotes_executed = Counter("fx_quotes_executed_total", "Total FX quotes executed")
quotes_expired = Counter("fx_quotes_expired_total", "Quotes that expired before execution")
quote_errors = Counter("fx_quote_errors_total", "Quote/execute errors", ["error_code"])
rate_fetch_success = Counter("fx_rate_fetch_success_total", "Successful rate refreshes")
rate_fetch_failure = Counter("fx_rate_fetch_failure_total", "Failed rate refreshes")
rates_stale_gauge = Gauge("fx_rates_stale", "1 if current rates are stale, 0 otherwise")


@router.get("/healthz", response_model=HealthResponse)
async def healthz():
    components: dict[str, HealthComponent] = {}
    overall = "ok"

    # DB check
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        components["database"] = HealthComponent(status="ok")
    except Exception as exc:
        components["database"] = HealthComponent(status="unhealthy", detail=str(exc))
        overall = "unhealthy"

    # Rates check
    stale = rate_provider.is_stale()
    rates_stale_gauge.set(1 if stale else 0)
    last = rate_provider.last_updated()
    if stale:
        components["rates"] = HealthComponent(
            status="degraded",
            detail=f"Stale since {last.isoformat() if last else 'never'}",
        )
        if overall == "ok":
            overall = "degraded"
    else:
        components["rates"] = HealthComponent(
            status="ok",
            detail=f"Last updated {last.isoformat() if last else 'never'}",
        )

    return HealthResponse(status=overall, components=components)


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(
        content=generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )
