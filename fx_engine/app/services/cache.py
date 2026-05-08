"""
Redis cache — shared rate storage across all API workers.

All operations are best-effort: a Redis failure never surfaces to the
caller — the app degrades gracefully to its in-memory fallback.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

log = logging.getLogger(__name__)

_redis: Optional[aioredis.Redis] = None

_CACHE_TTL_BUFFER_S = 120


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def cache_rates(mids: dict[str, Decimal]) -> None:
    """Persist mid-rates to Redis after a successful API refresh."""
    try:
        r = await get_redis()
        payload = json.dumps({k: str(v) for k, v in mids.items()})
        ttl = settings.rate_stale_seconds + _CACHE_TTL_BUFFER_S
        await r.setex("fx:rates:mids", ttl, payload)
    except Exception as exc:
        log.warning("Redis rate cache write failed: %s", exc)


async def load_cached_rates() -> Optional[dict[str, Decimal]]:
    """Return mid-rates from Redis, or None if unavailable / expired."""
    try:
        r = await get_redis()
        raw = await r.get("fx:rates:mids")
        if raw:
            return {k: Decimal(v) for k, v in json.loads(raw).items()}
    except Exception as exc:
        log.warning("Redis rate cache read failed: %s", exc)
    return None
