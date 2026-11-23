# Masader

Masader turns a research question about recent events into a short English brief in which **every finding
carries an evidence label** (corroborated, single-source, disputed, insufficient) and links to the source
passages it rests on. A rules layer and an output validator check every sentence against the cited evidence
before the brief is delivered. Reviewers approve publication; corrections create new versions.

This repository is the course implementation of the Masader Proposal (19 Oct 2026) and Technical
Architecture v1.2 (28 Oct 2026). Differences from the architecture are listed with reasons in
[docs/deviations.md](docs/deviations.md).

> **Important: what is real and what is simulated.**
> By default (`MODEL_MODE=simulated`) every model call is answered by a deterministic **heuristic
> simulator**, not by Claude. The simulator reads the real stage inputs and returns schema-valid output, so
> the whole system (queues, leases, budgets, rules, validator, rendering, recovery) runs for real, but the
> wording and judgement in simulated briefs are **not model outputs**. Simulated runs record model names as
> `sim:<model>` and every simulated brief shows a banner. The evaluation corpus is **synthetic**: 37 items
> from fictional outlets marked "(fixture)". `MODEL_MODE=live` uses the Anthropic API through the same
> gateway; it is implemented but has not been run against the real API in this build.

## Contents

* [Architecture](#architecture) · [Install](#install) · [Configure](#configure) · [Run](#run)
* [Use the API](#use-the-api) · [Tests](#tests) · [Evaluation](#evaluation) · [Deploy with Docker Compose](#deploy-with-docker-compose)
* [Troubleshooting](#troubleshooting) · [Repository layout](#repository-layout)

## Architecture

![Masader architecture as implemented](docs/architecture.png)

Source: [docs/architecture.mmd](docs/architecture.mmd). A request goes through eight stages, each a Celery
task that takes an atomic claim on its `report_stages` row, does its work, and commits its rows in the same
transaction, fenced on its lease:

`collect → dedup → extract → verify → label → write → validate → render`

* **cpu stages** (worker-cpu): collect (hybrid keyword + vector retrieval), dedup (exact, near-duplicate and
  origin grouping), label (evidence rules `rules-1.1.1`), render (JSON + HTML).
* **llm stages** (worker-llm): extract claims, verify them against other sources, write the brief, validate
  every sentence (rule checks + an independent support check by a different model than the writer).
* **gateway**: every call reserves its worst-case cost first (a database CHECK makes overspending
  impossible), retries once on fast errors, repairs invalid JSON once, falls back to a second model, opens
  a circuit breaker after 5 failures, and can cancel or abandon stuck calls.
* **reaper** (beat, every 60 s): recovers expired leases, lost calls and stuck reports.

## Install

Requirements: Python 3.12 or 3.13, PostgreSQL 16 with the pgvector extension, Redis 7. Docker is the easiest
way to get the two services.

```bash
git clone <this repository> masader && cd masader
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt     # all versions are pinned
make services        # starts pgvector/pgvector:pg16 on :5432 and redis:7.4-alpine on :6379
```

## Configure

Settings come from environment variables (see [.env.example](.env.example)); the defaults suit local
development with `make services`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql://masader:masader@localhost:5432/masader` | PostgreSQL with pgvector |
| `REDIS_URL` | `redis://localhost:6379/0` | broker and rate limits |
| `MODEL_MODE` | `simulated` | `simulated` (no key, heuristic outputs) or `live` (Anthropic API) |
| `ANTHROPIC_API_KEY` | empty | only for `live`; keep it in `.env` or a secret manager, never in Git |
| `KEY_PEPPER` | dev value | HMAC pepper for stored API keys; **must** be set when `ENV=prod` |
| `EMBEDDER` | `hashing` | `hashing` (offline substitute) or `fastembed` (multilingual MiniLM, downloads a model) |
| `LIVE_SOURCES` | `0` | `1` schedules the real GDELT, RSS and World Bank connectors |
| `SIM_LATENCY_SCALE` | `0` | simulated call latency = the role's time budget in minutes × this many seconds |
| `ADMISSION_MAX_ACTIVE` | `6` | reports in progress before new requests get `429 busy` |

Model routing, budgets, evidence-rule thresholds and the source register are in [config/](config/). The
timers must keep the order call timeout < stage deadline < soft limit < hard limit < lease < visibility
timeout (110 < 234 < 240 < 270 < 300 < 360 s); the app refuses to start otherwise.

## Run

```bash
make migrate seed                       # schema, sources, budgets, synthetic corpus
python -m masader.cli key create "Me" me@example.org admin --rate 100    # prints an API key once
make run                                # API + UI on http://127.0.0.1:8000
make worker                             # in a second terminal
make beat                               # in a third terminal (reaper, retention)
```

Open http://127.0.0.1:8000, paste the key, and press **Request brief**. The form is pre-filled with the
corpus example (shipping through the Strait of Hormuz, 12–19 Oct 2026).

## Use the API

The contract is [docs/openapi.v1.json](docs/openapi.v1.json) (also live at `/docs`). Every `/v1` route needs
`X-API-Key`. Keys have one of three roles: requester, reviewer, admin.

```bash
KEY=rq_...   # your key
curl -s localhost:8000/v1/requests -H "X-API-Key: $KEY" -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' -d '{
    "topic": "Commercial shipping through the Strait of Hormuz",
    "questions": ["What has happened to commercial shipping in the last 7 days, and how have Gulf and Indian officials responded?"],
    "from": "2026-10-12", "to": "2026-10-19", "geography": ["OMN","ARE","IND"]}'
# -> {"status":"needs_clarification","questions":[...],"answers_url":"/v1/requests/req_.../answers"}

curl -s localhost:8000/v1/requests/req_.../answers -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}'
# -> 202 {"report_id":"rep_...","status":"queued","status_url":"/v1/reports/rep_..."}   ({} accepts the defaults)

curl -s localhost:8000/v1/reports/rep_... -H "X-API-Key: $KEY"        # status, stages, then the brief
curl -s localhost:8000/v1/reports/rep_....html -H "X-API-Key: $KEY" > brief.html
curl -s -X POST localhost:8000/v1/reports/rep_.../publish -H "X-API-Key: $REVIEWER_KEY"   # public at /r/rep_...
```

Other routes: `GET /v1/review/queue` and `POST /v1/review/{id}` (reviewers), `POST /v1/reports/{id}/corrections`
and `POST /v1/corrections/{id}/resolve` (a correction creates version n+1), `POST /v1/reports/{id}/withdraw`,
`GET /v1/me`, `POST|DELETE /v1/keys`, `/healthz`, `/readyz`, `/metrics`.

Command line (`python -m masader.cli ...`): `migrate`, `seed [--fixtures]`, `key create|revoke`,
`delete-user EMAIL`, `reap`, `retention`, `openapi`.

## Tests

```bash
make test-fast     # unit + AI regression tests, no services needed
make test          # everything: unit, integration (PostgreSQL + Redis) and end-to-end
make cov           # with coverage
make lint
make security      # bandit (static analysis) and pip-audit (known-vulnerable dependencies)
```

Tests create and drop their own database `masader_test` and use Redis database 15, so they do not touch
development data. The end-to-end tests (`tests/e2e`) start real Uvicorn, Celery worker and beat processes,
kill a worker with SIGKILL mid-stage, hang a call thread that ignores cancellation, and hang a stage past its
soft limit, and check that the report is still delivered exactly once.

### Quality gates in CI and on GitHub

Every push and pull request runs, in GitHub Actions: **pre-commit** hooks (`make precommit`: ruff,
gitleaks, YAML/JSON/whitespace checks), a **gitleaks** scan of the whole history, ruff, **bandit**,
**pip-audit**, the test suite with an 80% coverage floor, the OpenAPI contract diff, the Docker image build
and smoke test, a **Trivy** scan of the image and of the Dockerfile/Compose file (fails on fixable HIGH or
CRITICAL findings), and a CycloneDX **SBOM** of the image (kept as a build artifact). **CodeQL** code
scanning runs on push, pull request and weekly; **Dependabot** opens weekly update pull requests for pip,
GitHub Actions and the Docker base image.

* **Releases:** pushing a tag `v*` publishes the image to GitHub Container Registry
  (`ghcr.io/maryamrra90-create/masader`) with a signed build-provenance attestation, an SBOM attestation
  and a GitHub release (`.github/workflows/release.yml`).
* **Documentation site:** https://maryamrra90-create.github.io/masader/ (MkDocs, built from this README and
  `docs/`, with an interactive API reference; `make docs-serve` to preview).

## Evaluation

Scripts in [eval/](eval/) write raw results with the environment and commit id to `eval/results/`.

| Script | Measures |
|--------|----------|
| `eval/quality.py` | origin-grouping precision/recall vs ground truth, evidence rules (A15), clarification questions, prompt injection |
| `eval/seeded_errors.py` | validator detection of seeded errors (hand-written E3 set and errors generated from a pipeline report) |
| `eval/drills.py` | failure drills: provider down, invalid JSON, validator independence, budget cap, Redis down, PostgreSQL down, worker kill, stuck threads |
| `eval/report_time.py` | report time and admission for a burst of 10 requests on real workers |
| `perf/run_load.py` | NFR1 read load: 50 Locust users for 3 minutes |

Results and their limits are summarised in the implementation report. All quality figures describe the
simulator and the synthetic corpus, not Claude on live news.

## Deploy with Docker Compose

```bash
cp .env.example .env        # set POSTGRES_PASSWORD, KEY_PEPPER (and GRAFANA_ADMIN_PASSWORD for monitoring)
docker compose up -d --build                         # postgres, redis, migrate, api, worker-llm, worker-cpu, scheduler
docker compose run --rm api masader key create "Admin" admin@example.org admin
docker compose --profile monitoring up -d            # Prometheus :9090, Grafana :3000, Jaeger :16686
MASADER_DOMAIN=example.org docker compose --profile edge up -d    # Caddy with automatic TLS
```

The API listens on 127.0.0.1:8000 only; publish it through the `edge` profile. Behind a TLS-intercepting
proxy, build with `docker build --secret id=ca,src=/path/to/ca.crt --network host .`.

### Distributed tracing (OpenTelemetry)

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` in `.env` and start the `monitoring` profile. Each report
then has one trace in Jaeger (http://127.0.0.1:16686): the HTTP request, the Celery publish and run span
of each of the eight stages, every database query, and an `llm.complete` / `llm.call` span per model call
with GenAI attributes (model, input and output tokens, outcome, fallback, estimated cost; never prompt or
source text). The `X-Trace-Id` response header and the `trace_id` in error bodies are the trace id to
search for. Tracing is off when the variable is empty. Code: `masader/telemetry.py`; recorded run:
`eval/results/tracing.json` and `docs/tracing-jaeger.png`.

![One report traced across the API and the Celery worker in Jaeger](docs/tracing-jaeger.png)

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `ValueError: timer order violated` at start | A timer env variable breaks the required order; restore the defaults or keep the order above. |
| `MODEL_MODE=live needs ANTHROPIC_API_KEY` | Set the key in `.env`, or use `MODEL_MODE=simulated`. |
| `/readyz` returns 503 | PostgreSQL or Redis is not reachable; the JSON says which. `make services`, check `DATABASE_URL` / `REDIS_URL`. |
| `POST /v1/requests` returns 503 `queue_unavailable` | Redis is down; nothing was stored, resend later with the same Idempotency-Key. |
| `429 busy` with `Retry-After: 60` | Six reports are already in progress (admission limit); retry after the given seconds. |
| `429 rate_limited` | The key's hourly request limit is used up. |
| `429 budget_exhausted` | The purpose's AI budget for the period is spent (`budgets` table). |
| Report stays `waiting_provider` | Both models failed; the stage retries every 60 s and the circuit breaker re-tests after 60 s. |
| Report goes to `in_review` | The validator removed too much, a stage failed twice, no independent validator model was available, or the topic is sensitive; a reviewer decides at `/v1/review/{id}`. |
| `could not open extension control file ... vector` | The database lacks pgvector; use the `pgvector/pgvector:pg16` image. |
| Arabic and English copies are not grouped | Expected with `EMBEDDER=hashing`; use `fastembed` where the model can be downloaded. |
| `pip install` fails TLS behind a proxy | Set `PIP_CERT` to your proxy CA, or use the Docker build secret above. |

## Repository layout

```
masader/            application package
  api/              FastAPI app, models, planner, minimal UI
  pipeline/         Celery app, stage protocol (kit.py), stages/, retention
  gateway/          model client, budget reservation, providers (live + simulated)
  rules/            evidence rules and assertion checks (rules-1.1.1)
  processing/       ingest, retrieval, embeddings, origin groups, text, redaction
  connectors/       GDELT, RSS, World Bank, synthetic fixture
  render/           Jinja2 brief template
  simulated/        heuristic stand-in for the model roles (labelled)
  store/            database pool, SQL migrations, object store
config/             routing, budgets, rules, sources, gazetteer
prompts/            versioned role prompts used in live mode
data/sample/        synthetic corpus and its generator
tests/              unit, integration, ai, e2e
eval/               evaluation scripts and results/
perf/               Locust load test
ops/                Prometheus, Grafana, Caddy configuration
docs/               architecture diagram, deviations, OpenAPI contract, report material
```

## Licence and responsible use

MIT. Briefs are AI-generated summaries of what sources report; read the cited evidence before relying on a
finding. The input policy refuses requests to profile private individuals.
