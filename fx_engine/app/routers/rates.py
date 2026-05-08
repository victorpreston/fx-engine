from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException

from app.rates import rate_provider
from app.schemas import RatesResponse, RatePairDetail, RefreshResponse

router = APIRouter(prefix="/rates", tags=["rates"])
log = structlog.get_logger(__name__)


@router.get("", response_model=RatesResponse)
async def get_rates():
    snap = rate_provider.snapshot()
    return {
        "pairs": {pair: RatePairDetail(**vals) for pair, vals in snap.items()},
        "last_updated": rate_provider.last_updated(),
        "source": "exchangeratesapi.io",
        "is_stale": rate_provider.is_stale(),
    }


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_rates():
    try:
        await rate_provider.refresh()
    except Exception as exc:
        log.warning("manual rate refresh failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail=f"Rate refresh failed: {exc}",
        )

    snap = rate_provider.snapshot()
    return {
        "status": "ok",
        "updated_at": rate_provider.last_updated(),
        "pairs_loaded": len(snap),
    }
