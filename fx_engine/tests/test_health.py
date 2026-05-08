"""Tests for /healthz and /metrics endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


async def test_healthz_returns_ok(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded", "unhealthy")
    assert "database" in data["components"]
    assert "rates" in data["components"]


async def test_healthz_database_component_ok(client):
    resp = await client.get("/healthz")
    assert resp.json()["components"]["database"]["status"] == "ok"


async def test_healthz_rates_stale_shows_degraded(client, monkeypatch):
    from app.rates import rate_provider
    from app.config import settings

    monkeypatch.setattr(
        rate_provider,
        "_fetched_at",
        datetime.now(timezone.utc)
        - timedelta(seconds=settings.rate_stale_seconds + 60),
    )
    resp = await client.get("/healthz")
    assert resp.json()["components"]["rates"]["status"] == "degraded"


async def test_metrics_endpoint_returns_prometheus_format(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "fx_quotes_created_total" in resp.text
    assert "fx_quotes_executed_total" in resp.text
    assert "fx_rates_stale" in resp.text
