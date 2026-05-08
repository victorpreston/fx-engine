from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.models.shared import HealthComponent, HealthResponse
from app.providers.rates import rate_provider
from app.services.database import get_pool
from app.services.metrics import rates_stale_gauge

router = APIRouter(tags=["observability"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz():
    components: dict[str, HealthComponent] = {}
    overall = "ok"

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        components["database"] = HealthComponent(status="ok")
    except Exception as exc:
        components["database"] = HealthComponent(status="unhealthy", detail=str(exc))
        overall = "unhealthy"

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
