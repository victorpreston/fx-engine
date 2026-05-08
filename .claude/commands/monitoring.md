# Start Monitoring Stack

Grafana and Prometheus only run inside Docker Compose — they are not available when running uvicorn manually.

## Start the full stack (API + DB + Redis + RabbitMQ + Prometheus + Grafana)
```bash
cd fx_engine && docker compose up --build
```

## URLs once running
| Service | URL | Credentials |
|---|---|---|
| API | http://localhost:8000 | — |
| Grafana dashboard | http://localhost:3000 | admin / admin |
| Prometheus | http://localhost:9090 | — |
| RabbitMQ management | http://localhost:15672 | fx / fx_secret |
| Raw metrics | http://localhost:8000/metrics | — |

## Grafana dashboard
The **FX Engine** dashboard is auto-provisioned — no manual import needed.
Go to http://localhost:3000 → login → Dashboards → FX Engine.

Panels: quote throughput, errors by type, API latency P50/P95/P99, rate provider health, request rate by endpoint.

## Monitoring config location
```
fx_engine/monitoring/
├── prometheus/
│   └── prometheus.yml     scrape config (scrapes api:8000/metrics every 15s)
└── grafana/
    ├── provisioning/      auto-provision datasource + dashboard on startup
    └── dashboards/
        └── fx-engine.json dashboard definition
```

## Generate traffic to see the dashboard populate
After `docker compose up`, run the smoke test:
```bash
cd fx_engine && bash -c "$(cat .claude/commands/smoke-test.md | grep -A 100 '```bash' | tail -n +2 | head -n -1)"
```
Or use the Postman collection: `FX-Engine.postman_collection.json` → Smoke Flow folder → Run.
