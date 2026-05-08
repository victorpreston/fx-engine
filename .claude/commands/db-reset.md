# Reset Database

Tears down and recreates the test database container with a clean schema.

```bash
cd fx_engine && docker compose -f docker-compose.test.yml down -v && docker compose -f docker-compose.test.yml up -d
```

Then wait ~3 seconds for PostgreSQL to become healthy, and migrations will run automatically the next time the API or test suite starts.

## Verify it's ready
```bash
docker compose -f docker-compose.test.yml ps
```
