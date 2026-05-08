"""
Prometheus metrics registry.

Centralised here so fx_engine.py, rates.py, and health.py can all
import and increment the same counter objects without circular imports.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

quotes_created = Counter("fx_quotes_created_total", "Total FX quotes generated")
quotes_executed = Counter("fx_quotes_executed_total", "Total FX quotes executed")
quotes_expired = Counter(
    "fx_quotes_expired_total", "Quotes that expired before execution"
)
quote_errors = Counter(
    "fx_quote_errors_total", "Quote/execute errors by error code", ["error_code"]
)
rate_fetch_success = Counter(
    "fx_rate_fetch_success_total", "Successful upstream rate refreshes"
)
rate_fetch_failure = Counter(
    "fx_rate_fetch_failure_total", "Failed upstream rate refreshes"
)
rates_stale_gauge = Gauge(
    "fx_rates_stale", "1 if current rates exceed the staleness threshold, else 0"
)

request_duration = Histogram(
    "fx_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
