# Run Tests

Runs the full test suite against the local test database.

## Start the test database (first time only)
```bash
cd fx_engine && docker compose -f docker-compose.test.yml up -d
```

## Run all tests
```bash
cd fx_engine && TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db pytest -v --tb=short
```

## Run a single file
```bash
cd fx_engine && TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db pytest tests/test_execute.py -v
```

## Run a single test
```bash
cd fx_engine && TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db pytest tests/test_execute.py::test_execute_debits_source_and_credits_destination -v
```

## Run concurrency tests only
```bash
cd fx_engine && TEST_DATABASE_URL=postgresql://fx_test:fx_test_secret@localhost:5433/fx_test_db pytest tests/test_concurrency.py tests/test_idempotency.py -v
```
