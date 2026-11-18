# Common tasks. `make help` lists them. Local runs expect PostgreSQL (pgvector) on :5432 and Redis on :6379,
# e.g. from `make services`.
PY ?= python
export PYTHONPATH := $(CURDIR)

.PHONY: help install services migrate seed run worker beat test test-fast e2e cov lint eval openapi docker up down
help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'
install:         ## install pinned runtime and dev dependencies
	$(PY) -m pip install -r requirements.txt -r requirements-dev.txt
services:        ## start PostgreSQL+pgvector and Redis containers for local development
	docker run -d --name mpg -e POSTGRES_USER=masader -e POSTGRES_PASSWORD=masader -e POSTGRES_DB=masader -p 5432:5432 pgvector/pgvector:pg16
	docker run -d --name mredis -p 6379:6379 redis:7.4-alpine
migrate:         ## apply database migrations
	$(PY) -m masader.cli migrate
seed:            ## load sources, budgets and the synthetic corpus
	$(PY) -m masader.cli seed --fixtures
run:             ## API on http://127.0.0.1:8000 (UI at /)
	$(PY) -m uvicorn masader.api.app:app --port 8000 --reload
worker:          ## one Celery worker for all queues (development)
	$(PY) -m celery -A masader.pipeline.celery_app worker -Q llm,cpu,ingest -P prefork -c 4 --loglevel INFO
beat:            ## Celery beat (reaper)
	$(PY) -m celery -A masader.pipeline.celery_app beat --loglevel INFO
test:            ## all tests, including real-worker end-to-end tests
	$(PY) -m pytest -q
test-fast:       ## unit tests only (no services needed)
	$(PY) -m pytest -q tests/unit tests/ai
e2e:             ## end-to-end tests over uvicorn + Celery
	$(PY) -m pytest -q tests/e2e
cov:             ## coverage report (target >= 80%)
	$(PY) -m pytest -q --cov=masader --cov-report=term-missing --cov-report=xml
lint:            ## ruff
	$(PY) -m ruff check masader tests eval perf
eval:            ## evaluation scripts; results in eval/results/
	cd eval && $(PY) quality.py && $(PY) seeded_errors.py && $(PY) drills.py && $(PY) report_time.py
openapi:         ## regenerate docs/openapi.v1.json
	$(PY) -m masader.cli openapi > docs/openapi.v1.json
docker:          ## build the image
	docker build -t masader:1.0.0 .
up:              ## start the compose stack (needs .env)
	docker compose up -d --build
down:            ## stop the compose stack
	docker compose down
