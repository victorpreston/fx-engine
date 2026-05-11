"""Tests for /healthz, /readyz, and /metrics endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


async def test_healthz_is_liveness_only(client):
    """healthz must return ok unconditionally — no DB or rate check."""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_healthz_returns_ok_even_with_stale_rates(client, monkeypatch):
    """healthz must NOT fail when rates are stale — that is readyz's job."""
    from app.config import settings
    from app.providers.rates import rate_provider

    monkeypatch.setattr(
        rate_provider,
        "_fetched_at",
        datetime.now(timezone.utc) - timedelta(seconds=settings.rate_stale_seconds + 60),
    )
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readyz_returns_ready_with_fresh_rates(client):
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["database"] == "ok"
    assert data["rates"] == "ok"


async def test_readyz_not_ready_when_rates_stale(client, monkeypatch):
    """readyz must return 503 when rates are stale — load balancer pulls it from rotation."""
    from app.config import settings
    from app.providers.rates import rate_provider

    monkeypatch.setattr(
        rate_provider,
        "_fetched_at",
        datetime.now(timezone.utc) - timedelta(seconds=settings.rate_stale_seconds + 60),
    )
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "not_ready"
    assert data["database"] == "ok"
    assert "stale" in data["rates"]


async def test_metrics_endpoint_returns_prometheus_format(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "fx_quotes_created_total" in resp.text
    assert "fx_quotes_executed_total" in resp.text
    assert "fx_rates_stale" in resp.text
