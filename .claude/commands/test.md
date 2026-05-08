# Run Tests

Requires the test PostgreSQL container running on port 5433.

## Start the test database (first time / after reset)
```bash
cd fx_engine && docker compose -f docker-compose.test.yml up -d
```

## Run all migrations against the test database
```bash
cd fx_engine && DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db alembic upgrade head
```

## Run full test suite
```bash
cd fx_engine && TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db pytest -v
```

## Run a single test file
```bash
cd fx_engine && TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db pytest tests/test_execute.py -v
```

## Run a single test
```bash
cd fx_engine && TEST_DATABASE_URL=... pytest tests/test_execute.py::test_execute_debits_source_and_credits_destination -v
```

## Run with coverage report
```bash
cd fx_engine && TEST_DATABASE_URL=... pytest --cov=app --cov-report=term-missing
```

## Lint and format checks (mirrors CI)
```bash
cd fx_engine && python -m ruff check app/ tests/ && python -m ruff format --check app/ tests/
```
