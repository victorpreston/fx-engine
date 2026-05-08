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
from app.database import close_pool, get_pool, run_migrations
from app.exceptions import FXError
from app.rates import rate_provider
from app.routers import customers, health, quotes, rates as rates_router

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
    pool = await get_pool()
    await run_migrations(pool)
    await rate_provider.start()
    log.info("startup_complete")
    yield
    log.info("shutdown")
    await rate_provider.stop()
    await close_pool()
    log.info("shutdown_complete")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="FX Engine",
    description="Foreign exchange engine — quotes, execution, and customer balances.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ── Pure ASGI observability middleware ────────────────────────────────────────
class ObservabilityMiddleware:
    """
    Attaches a request-scoped trace ID, logs every request, and injects
    X-Request-ID into responses.

    Implemented as a raw ASGI callable (not BaseHTTPMiddleware) so it
    never spawns tasks or creates futures — the only async work is
    delegating to the inner app and forwarding messages.
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
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            log.info(
                "request_completed",
                status_code=status_code,
                duration_ms=duration_ms,
            )


app.add_middleware(ObservabilityMiddleware)


# ── Global exception handlers ─────────────────────────────────────────────────
@app.exception_handler(FXError)
async def fx_error_handler(request: Request, exc: FXError) -> JSONResponse:
    ctx = structlog.contextvars.get_contextvars()
    request_id = ctx.get("request_id", "")
    log.warning("fx_error", error_code=exc.error_code, detail=str(exc))
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
app.include_router(rates_router.router)
app.include_router(health.router)
