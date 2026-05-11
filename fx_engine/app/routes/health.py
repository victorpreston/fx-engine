from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.providers.rates import rate_provider
from app.services.database import get_pool
from app.services.metrics import rates_stale_gauge

router = APIRouter(tags=["observability"])


@router.get("/healthz")
async def healthz():
    """Process liveness only — no DB or rate check.

    Load balancers and container orchestrators use this probe to decide
    whether to restart the process.  Checking DB or rate freshness here
    would cause a provider outage to trigger unnecessary restarts.
    """
    return {"status": "ok"}


@router.get("/readyz")
async def readyz():
    """Readiness for quote traffic: DB reachable + rates fresh.

    Returns 503 when the service should be pulled out of rotation —
    stale rates or unreachable DB — without triggering a process restart.
    """
    db_status = "ok"
    rates_status = "ok"

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        db_status = f"unhealthy: {exc}"

    stale = rate_provider.is_stale()
    rates_stale_gauge.set(1 if stale else 0)
    if stale:
        last = rate_provider.last_updated()
        rates_status = f"stale since {last.isoformat() if last else 'never'}"

    ready = db_status == "ok" and rates_status == "ok"
    body = {
        "status": "ready" if ready else "not_ready",
        "database": db_status,
        "rates": rates_status,
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(
        content=generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )
