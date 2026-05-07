"""
FX Engine — FastAPI application entry point.

Request lifecycle:
  1. Middleware attaches a request-scoped trace ID (X-Request-ID header or auto-generated UUID).
  2. structlog binds the request ID and route metadata to every log record in the request scope.
  3. Routers delegate to fx_engine.py for all business logic.
  4. FX-specific exceptions are mapped to structured HTTP error responses.
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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


# ── Middleware ────────────────────────────────────────────────────────────────
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    log.info(
        "request_completed",
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    response.headers["X-Request-ID"] = request_id
    return response


# ── Global exception handlers ─────────────────────────────────────────────────
@app.exception_handler(FXError)
async def fx_error_handler(request: Request, exc: FXError):
    request_id = request.headers.get("X-Request-ID", "")
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
async def unhandled_error_handler(request: Request, exc: Exception):
    request_id = request.headers.get("X-Request-ID", "")
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
