"""
FX Engine — FastAPI application entry point.

Middleware note: we use a raw ASGI middleware class instead of
Starlette's BaseHTTPMiddleware deliberately.  BaseHTTPMiddleware spawns
anyio task groups whose futures get bound to the anyio-managed event
loop, which conflicts with asyncpg's pool (bound to the asyncio loop)
under concurrent load.  The pure ASGI class wraps `send` directly —
no tasks, no futures, no cross-loop confusion.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.exceptions import FXError
from app.providers.rates import rate_provider
from app.routes import customers, health, quotes, transactions
from app.routes import rates as rates_router
from app.services.cache import close_redis, get_redis
from app.services.database import close_pool, get_pool
from app.services.events import close_events, connect_events
from app.services.metrics import quote_errors, request_duration

# ── Structured logging ────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}.get(
            settings.log_level, 20
        )
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger(__name__)


# ── Application lifespan ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", environment=settings.environment)
    await get_pool()
    try:
        await get_redis()
        log.info("redis_connected")
    except Exception as exc:
        log.warning("redis_unavailable", error=str(exc))
    await connect_events()
    await rate_provider.start()
    log.info("startup_complete")
    yield
    log.info("shutdown")
    await rate_provider.stop()
    await close_events()
    await close_redis()
    await close_pool()
    log.info("shutdown_complete")


# ── FastAPI app ───────────────────────────────────────────────────────────────
_is_production = settings.environment == "production"

app = FastAPI(
    title="FX Engine",
    description="Foreign exchange engine — quotes, execution, and customer balances.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)


# ── Pure ASGI observability middleware ────────────────────────────────────────
class ObservabilityMiddleware:
    """
    Attaches a request-scoped trace ID, logs every request, records
    Prometheus latency histogram, and injects X-Request-ID into responses.
    """

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        raw_headers: dict[bytes, bytes] = dict(scope.get("headers", []))
        request_id = raw_headers.get(b"x-request-id", b"").decode() or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=scope.get("method", ""),
            path=scope.get("path", ""),
        )

        start = time.monotonic()
        status_code = 500

        async def send_with_trace(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers_out = list(message.get("headers", []))
                headers_out.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers_out}
            await send(message)

        try:
            await self.inner(scope, receive, send_with_trace)
        finally:
            duration = time.monotonic() - start
            log.info(
                "request_completed",
                status_code=status_code,
                duration_ms=round(duration * 1000, 2),
            )
            request_duration.labels(
                method=scope.get("method", ""),
                path=scope.get("path", ""),
            ).observe(duration)


app.add_middleware(ObservabilityMiddleware)


# ── Global exception handlers ─────────────────────────────────────────────────
@app.exception_handler(FXError)
async def fx_error_handler(request: Request, exc: FXError) -> JSONResponse:
    ctx = structlog.contextvars.get_contextvars()
    request_id = ctx.get("request_id", "")
    log.warning("fx_error", error_code=exc.error_code, detail=str(exc))
    quote_errors.labels(error_code=exc.error_code).inc()
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": str(exc),
            "error_code": exc.error_code,
            "request_id": request_id,
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    ctx = structlog.contextvars.get_contextvars()
    request_id = ctx.get("request_id", "")
    log.exception("unhandled_error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "An unexpected error occurred",
            "error_code": "internal_error",
            "request_id": request_id,
        },
    )


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(quotes.router)
app.include_router(customers.router)
app.include_router(transactions.router)
app.include_router(rates_router.router)
app.include_router(health.router)
