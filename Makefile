# Local test run. Needs a Vicoa checkout and a Postgres migrated with its schema.
VICOA_SRC ?= ../vicoa/backend/src
TEST_DATABASE_URL ?= postgresql://vicoa:vicoa@127.0.0.1:5432/vicoa_overlay_test
PY ?= python

.PHONY: test unit migrate-test-db

unit:
	$(PY) -m pytest -q -m "not integration" tests

test:
	VICOA_SRC=$(abspath $(VICOA_SRC)) TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PY) -m pytest -q tests

migrate-test-db:
	cd $(VICOA_SRC)/shared && DATABASE_URL=$(TEST_DATABASE_URL) ENVIRONMENT=test PYTHONPATH=$(abspath $(VICOA_SRC)) alembic upgrade head
