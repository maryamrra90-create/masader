# Engineering toolchain and AI use

**Project:** Masader, implementation deliverable · **Repository:** https://github.com/maryamrra90-create/masader (commit `a1ae607`, release `v1.0.0`)

This section lists every tool that the repository, its CI configuration, its pinned requirements, the
Compose file and the recorded evaluation outputs show was **actually used**. Each entry says what the tool
does in Masader, why it was chosen, and where the evidence is. Paths are relative to the repository root
unless they start with `architecture/` (the architecture submission package) or `demo/` (the
implementation package). Where a tool is wired in but was **not exercised** in this build, the entry says
so; those are marked *Integrated, not exercised*.

The section has three parts, kept separate on purpose:

* **A. AI assistance used to build the project** (what helped MRA write it)
* **B. AI inside the product** (what Masader itself calls at run time)
* **C. Non-AI engineering tools** (everything else)

---

## A. AI assistance used to build the project

| Tool | What it did | How its output was checked | Evidence |
|---|---|---|---|
| **Claude (Anthropic), through Claude Code** | Generated most of the source code, tests, evaluation scripts, deployment files and documentation from MRA's Proposal and Architecture v1.2, under MRA's direction. Ran the test suite, the evaluations, the security scans and the Compose stack, and reported the measured results. Also wrote the narration script for the demo video. | Every number in the report comes from a script in the repository that was run, with its raw output and commit id in `eval/results/`. 139 automated tests, CI green on GitHub Actions, bandit and pip-audit clean. Deviations from the architecture are listed in `docs/deviations.md`. MRA reviews and accepts the deliverables. | `docs/declaration.md`; report Section 7; `eval/results/*.json` (each file records `git_commit`) |
| **Pico TTS (`pico2wave`, British English voice)** | Read the narration script aloud for `masader_demo_narrated.mp4`. It is a synthetic voice, not MRA's, and this is declared. | Transcript published next to the video so the words can be checked. | `perf/demo/record_narrated.js` (line 2), `perf/demo/narration/lines.json`, `demo/narration_transcript.md` |

> Pico TTS is a classic rule-based speech synthesiser rather than a generative model. It is listed here
> because it produced media that appears in the submission and is not the student's own voice.

---

## B. AI inside the product

Masader is an evidence-labelled research-brief system. Six **model roles** sit behind a single gateway.
No role imports a provider SDK; only the gateway does (`masader/gateway/providers.py`, top docstring).

### B.1 Model roles and routing

| Role | Primary model | Fallback | Job | Prompt |
|---|---|---|---|---|
| planner | claude-haiku-5-5 | none | turns a request into a plan and clarification questions | `prompts/planner.en.v1.0.md` |
| extractor | claude-sonnet-5-5 | claude-haiku-4-5 | pulls claims out of sources | `prompts/extractor.en.v1.0.md` |
| verifier | claude-sonnet-5-5 | claude-haiku-4-5 | checks each claim against its sources | `prompts/verifier.en.v1.0.md` |
| writer | claude-sonnet-5-5 | claude-haiku-4-5 | writes the brief | `prompts/writer.en.v1.0.md` |
| splitter | claude-haiku-5-5 | claude-haiku-4-5 | splits the brief into checkable sentences | `prompts/splitter.en.v1.0.md` |
| support | claude-haiku-5-5 | claude-haiku-4-5 | independent support check; never runs on the writer's model | `prompts/support.en.v1.0.md` |

Source: `config/routing.yaml`, which also holds per-model prices used for cost estimates and budgets.

### B.2 Providers

| Provider | Status in this build | What it does | Evidence |
|---|---|---|---|
| **Anthropic Messages API** via the official **`anthropic` Python SDK** (2.10.0) | *Integrated, not exercised live.* No API key was available while it was built, so it is unit-tested against a fake SDK client only. | `MODEL_MODE=live`. Uses JSON-schema **structured output** (`output_config.format = json_schema`) so replies are machine-checkable, and **prompt caching** (`cache_control: ephemeral` on the system prompt) to cut cost on repeated calls. | `masader/gateway/providers.py` lines 1–9 and ~145–151; `requirements.txt`; `tests/unit/test_providers.py` |
| **Deterministic heuristic simulator** (written for this project) | **Used** for every test, evaluation and demo. Its outputs are not model outputs and are labelled as such (the rendered HTML carries a "simulated" banner). | `MODEL_MODE=simulated`, the default. Computes outputs from the real stage input so the whole system runs without a key. Supports **fault injection** through Redis keys (timeout, error, invalid JSON, hang, refusal) for the failure drills. | `masader/simulated/heuristics.py`; `providers.py` docstring; `eval/results/compose_stack.json` (`html_has_simulated_banner: true`) |

### B.3 Gateway controls around the model

These are engineering controls Masader puts around every model call (all in `masader/gateway/`):

* **Schema validation** of every reply with `jsonschema`; invalid JSON is retried, then sent to review (`client.py` ~158).
* **Circuit breaker** per model, with fallback to the next model in the route (`circuit.py`, `config/routing.yaml`).
* **Budget reservation**: cost is reserved before a call and settled after, so spend never passes the cap; checked by the `budget_near_cap` drill (`budget.py`, `config/budgets.yaml`, `eval/results/drills.json`).
* **Prompt-injection control**: source text goes inside tagged `<source>` blocks with `<` and `>` escaped, so a source cannot close the block or open a fake one (`providers.py`, `_source_block`).
* **Versioned prompts** as files (`prompts/*.v1.0.md`), so a prompt change is a reviewed diff.

### B.4 Embeddings and retrieval

| Tool | Status | What it does | Evidence |
|---|---|---|---|
| **Hashing n-gram embedder** (written for this project, NumPy) | **Used** (default, `EMBEDDER=hashing`) | 384-dimension feature-hashing embedding for de-duplication. Needs no download. Documented substitution: it has no cross-lingual meaning. | `masader/processing/embed.py` |
| **fastembed / ONNX, paraphrase-multilingual-MiniLM-L12-v2** | *Integrated, not exercised* (Hugging Face was unreachable from the build environment) | The designed multilingual embedder (architecture ADR-08). Optional dependency. | `masader/processing/embed.py` docstring; `.env.example` |
| **Hybrid retrieval in PostgreSQL** | **Used** | Score = weighted exact cosine (pgvector `<=>`) + PostgreSQL full-text rank (`tsvector` with a GIN index), inside the request's date window. | `masader/processing/retrieve.py`; `masader/store/migrations/0001_architecture_schema.sql` |
| **Deterministic evidence rules** (no model) | **Used** | Rule engine that blocks findings without enough evidence; tested by seeded errors. | `masader/rules/evidence_rules.py`; `eval/seeded_errors.py`; `architecture/validation/E3_rules/` |

---

## C. Non-AI engineering tools

### C.1 Source control, collaboration and CI/CD (GitHub)

| Tool | What it does in Masader | Why | Evidence |
|---|---|---|---|
| **Git** | Version history with descriptive messages; feature work on branches merged through pull requests (#1, #6, #7, #10). | Every result file records the commit it was measured on, so numbers trace to code. | `git log`; `git_commit` key in `eval/results/*.json`; `masader.bundle` |
| **GitHub** (public repository) | Hosts the code and runs CI. | Course requires source code on GitHub with run instructions. | https://github.com/maryamrra90-create/masader |
| **GitHub Actions** | Workflow `ci` on every push and pull request. Job `test`: secret scan → ruff → bandit + pip-audit → pytest with an 80% coverage floor → OpenAPI contract diff. Job `image`: Docker build and smoke test. Uses **service containers** (pgvector Postgres 16, Redis 7.4) with health checks, and `actions/setup-python` pip caching. | One reproducible gate for quality, security and packaging; the same commands run locally with `make`. | `.github/workflows/ci.yml`; green on every commit since `48fb392` (https://github.com/maryamrra90-create/masader/actions/runs/45208172116), including `9c83868` |
| **CodeQL** (GitHub code scanning) | Semantic static analysis with the `security-extended` query suite for **Python**, the **JavaScript** demo recorders and the **GitHub Actions** workflow files. Runs on every push and pull request to `main` and weekly; results go to the repository's Security tab. Added in PR #1; all three analyses passed on the PR and on `main` at `9c83868`. | Finds data-flow bugs (injection, unsafe deserialisation, workflow script injection) that pattern linters such as Bandit miss. | `.github/workflows/codeql.yml`; https://github.com/maryamrra90-create/masader/pull/1 |
| **Dependabot** | Weekly update pull requests for **pip** (requirements files), **GitHub Actions** and the **Docker** base image, with minor and patch bumps grouped. Each update PR runs the full CI and CodeQL before it can be merged. On its first run (23 Nov 2026) it opened four PRs: Python packages (13 grouped updates), Actions (2), Redis client 6.4 → 8.1, and base image python 3.12-slim → 3.15-slim. These are reviewed, not merged automatically. | Pinned dependencies stay reproducible but do not go stale; vulnerable versions are flagged early. | `.github/dependabot.yml`; PRs #2–#5 |
| **pre-commit** 4.3 | The same fast checks before every local commit and as a CI job: ruff, gitleaks, YAML/JSON/TOML validity, whitespace and end-of-file, merge-conflict markers, private keys, files over 1 MB. | Problems are caught on the developer's machine, not only in CI. | `.pre-commit-config.yaml`; `make precommit`; job `pre-commit` in `ci.yml` (PR #6) |
| **GitHub Pages + MkDocs Material** | Documentation site built from the README and `docs/`: architecture with the diagram rendered live from its Mermaid source, an interactive **Redoc** API reference generated from `docs/openapi.v1.json`, this toolchain page, traceability and deviations. Pull requests build it with `--strict` (a broken link fails); `main` publishes it to the `gh-pages` branch. | One link for markers to browse the project. | `mkdocs.yml`; `.github/workflows/docs.yml`; `ops/docs/prepare.py`; `gh-pages` branch |
| **Release workflow (GitHub Container Registry)** | Builds the image and publishes it as `ghcr.io/maryamrra90-create/masader:<version>`, with a signed **build-provenance attestation** and an **SBOM attestation** (Sigstore, keyless), and a GitHub release with the SBOM attached. Third-party actions are pinned to commit SHAs (a CodeQL finding on PR #6). | Versioned, verifiable artefact anyone can `docker pull`. | `.github/workflows/release.yml`; **v1.0.0 released 23 Nov 2026**: https://github.com/maryamrra90-create/masader/releases/tag/v1.0.0 (SBOM attached; run https://github.com/maryamrra90-create/masader/actions/runs/45249666575) |
| **gitleaks** 8.24.3 (CLI) | Scans the **whole Git history** for secrets, with `--redact`; the build fails on any finding. | The action version only scans the pushed range and fails on a first push, so the CLI is used instead (commit `3d3d899`). | `.github/workflows/ci.yml` |

### C.2 Code quality, testing and security

| Tool | What it does | Evidence |
|---|---|---|
| **pytest** 9.1.1 + **pytest-cov** / **coverage** | 144 tests in four suites: unit 78, integration 59 (5 of them for tracing), AI-regression 3, end-to-end 4 (real Uvicorn + Celery worker processes). 93.7% line coverage at the last recorded run (139 tests); CI fails under 80%. Markers `db`, `worker`, `slow`. | `tests/`; `pyproject.toml`; `eval/results/tests_coverage.json` |
| **Ruff** 0.17.5 | Linter and import sorter with the `S` (security, flake8-bandit) and `B` (bugbear) rule sets enabled. | `pyproject.toml` `[tool.ruff]`; CI step |
| **Bandit** 1.8.6 | Static security analysis of `masader/`. It flagged unsafe XML parsing of RSS feeds, which led to the `defusedxml` fix. Now 0 findings. | `eval/results/security_bandit.json`; commit `b13255f` |
| **pip-audit** 2.9.0 | Checks pinned dependencies against known-vulnerability databases. Found 6 advisories in Starlette 0.48 (one high, CVE-2025-62727); fixed by upgrading to FastAPI 0.162 / Starlette 1.10. Now clean. | `eval/results/security_pip_audit.json`; `requirements.txt` comment |
| **Trivy** 0.76.1 | In the CI `image` job: scans the built image for vulnerabilities and secrets and the Dockerfile and Compose file for misconfigurations; the build fails on any **fixable HIGH or CRITICAL** finding. The binary is checksum-verified rather than run through a third-party action. **On its first run it found a real HIGH vulnerability (CVE-2026-85091, zlib1g) in the `python:3.12-slim` base image**; the Dockerfile now applies Debian security updates and the scan is clean. | `.github/workflows/ci.yml`; `Dockerfile`; PR #6 |
| **CycloneDX SBOM** (generated by Trivy) | A software bill of materials of the image (every OS and Python package with its version), kept as a CI artefact on every build and attached to releases. | `ci.yml` artefact `sbom-cyclonedx`; `release.yml` |
| **OpenAPI contract test** | `masader openapi` output is diffed against the committed `docs/openapi.v1.json` in CI, so an accidental API change fails the build. | `docs/openapi.v1.json`; `tests/unit/test_openapi_contract.py`; CI step |
| **Locust** 2.47.0 | Load test for NFR1: 50 users for 3 minutes, 0 failures, p95 of 11 ms on report reads (target under 2,000 ms). Also used in the architecture experiment E2. | `perf/locustfile.py`; `perf/run_load.py`; `eval/results/load_nfr1.json`; `architecture/validation/E2_async_pipeline/loadtest/` |
| **Failure drills** (written for this project) | Scripted chaos tests: worker crash, provider timeout, hangs, invalid JSON, budget at the cap, Redis down, Postgres down. | `eval/drills.py`; `eval/results/drills.json`; `architecture/validation/E2_async_pipeline/drills.py` |
| **Make** | One entry point for install, services, test, lint, security, eval, openapi, docker, up/down. | `Makefile` |

### C.3 Application runtime

| Tool | What it does | Why | Evidence |
|---|---|---|---|
| **Python 3.12** | Language and runtime (CI and image on 3.12; local measurement runs on 3.13). | Mature AI and web ecosystem. | `pyproject.toml`; `Dockerfile` |
| **FastAPI** 0.162 / **Starlette** 1.10 | The HTTP API (`/v1/...`) and the web UI; generates the OpenAPI document. | Typed request models and automatic OpenAPI. | `masader/api/app.py` |
| **Uvicorn** 0.54 | ASGI server, 2 workers in the image. | Standard production server for FastAPI. | `Dockerfile` `CMD` |
| **Pydantic** 2.13 | Request and response models; settings. | Validation at the API boundary. | `masader/api/models.py`; `masader/settings.py` |
| **Celery** 5.6 + **Kombu** 5.7 | Asynchronous pipeline: eight stages (collect, dedup, label, extract, verify, write, validate, render) on two queues: `llm` (8 prefork workers) and `cpu,ingest` (3). Celery **beat** runs the reaper every 60 s. Configured with `acks_late`, `reject_on_worker_lost`, `prefetch_multiplier=1` and soft/hard time limits, so a crashed stage is redelivered and a hung one is killed. | Reports take minutes; the API must answer at once and survive worker crashes. | `masader/pipeline/celery_app.py`; `compose.yaml` |
| **Redis** 7.4 (`redis-py` 6.4) | Celery broker (with append-only persistence), admission control, idempotency keys, fault-injection keys for drills. | One small service for queueing and fast shared state. | `compose.yaml`; `masader/redisclient.py` |
| **PostgreSQL 16 + pgvector** (`pgvector/pgvector:pg16`) via **psycopg 3** + **psycopg-pool** | System of record: requests, reports, items, claims, evidence links, budgets. `vector(384)` embeddings and a generated `tsvector` with a GIN index for hybrid search. Plain SQL migrations. | One database for relational, vector and full-text data; measured to hold a year of items in architecture experiment E1. | `masader/store/migrations/*.sql`; `masader/store/db.py`; `architecture/validation/E1_datastore/` |
| **NumPy** 2.5 | Embedding maths. | | `masader/processing/embed.py` |
| **jsonschema** 4.26 | Validates every model reply against its schema. | | `masader/gateway/client.py` |
| **Jinja2** 3.1 | Renders the HTML brief. | Autoescaping templates. | `masader/render/templates/brief.en.html.j2` |
| **httpx** 0.28 | HTTP client for the source connectors. | Timeouts and a clean API. | `masader/connectors/*.py` |
| **defusedxml** 0.7 | Safe parsing of untrusted RSS/Atom XML (closes Bandit B314). | | `masader/connectors/rss.py` |
| **PyYAML** | Configuration files (`config/*.yaml`). | | `masader/config.py` |
| **boto3** | S3 object store for production (local folder in development). *Integrated, not exercised.* | Architecture ADR-13. | `masader/store/objectstore.py` |

**Data sources** (connectors): GDELT, RSS/Atom feeds and the World Bank API (`masader/connectors/`). The
tests, evaluations and demo use the **synthetic corpus** and a World Bank fixture
(`data/sample/corpus.jsonl`, `data/sample/worldbank_fixture.json`); live fetching is off by default
(`LIVE_SOURCES=0`).

### C.4 Containers, deployment and operations

| Tool | What it does | Evidence |
|---|---|---|
| **Docker** | One image (`python:3.12-slim`, non-root user uid 10001, health check, build secret for a proxy CA that is not kept in the image) runs the API, both workers, the scheduler and the migrate job. Built in CI and smoke-tested. Image 455 MB, built in 38 s. | `Dockerfile`; `eval/results/compose_stack.json` |
| **Docker Compose** | Whole stack on one host: postgres, redis, a one-shot migrate job, api, worker-llm, worker-cpu, scheduler, with health checks and `depends_on` conditions. Profiles add monitoring and edge. Measured: ready after 11 s, one report delivered over HTTP, 770 MiB idle memory. | `compose.yaml`; `eval/results/compose_stack.json` |
| **Prometheus** v3.5 | Scrapes the API's `/metrics` (`prometheus-client`: request latency histogram, queue length, reports by status, budget left). Five alert rules: API down, API slow, LLM queue backlog, reports stuck in review, live budget low. Target was `up` in the Compose run. | `ops/prometheus/`; `masader/api/app.py` lines 33–38 |
| **Grafana** 12.1 | Provisioned dashboard and Prometheus data source. Answered HTTP 200 in the Compose run. | `ops/grafana/` |
| **Caddy** 2.10 | TLS reverse proxy with HSTS and security headers; hides `/metrics` from the public. *Configured, not run* in the recorded Compose test (the `edge` profile was not started). | `ops/caddy/Caddyfile`; `compose.yaml` |
| **OpenTelemetry** (SDK 1.45, instrumentation 0.66) | Distributed tracing, one trace per report: the FastAPI request, the Celery publish and run spans of all eight stages (context carried in the task headers), every psycopg query, and `llm.complete` / `llm.call` spans in the model gateway with **GenAI semantic-convention attributes** (model, input and output tokens) plus outcome, fallback and estimated cost. Prompts and source text are never put on spans. Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set; `X-Trace-Id` and error bodies return the trace id. Running it against Jaeger found that model calls made in the stage's thread pool lost the trace; they now run in a copy of the stage's context. | `masader/telemetry.py`; `masader/gateway/client.py`; `masader/pipeline/kit.py`; `tests/integration/test_tracing.py` (5 tests); PR #7 |
| **Jaeger** 2.22 | Trace collector and UI (OTLP/HTTP), in the Compose `monitoring` profile. Recorded run: one report traced through the API and a Celery worker, **442 spans across 2 services in 761 ms** (simulated models). | `compose.yaml`; `eval/results/tracing.json`; `docs/tracing-jaeger.png` |
| **pg_dump / pg_restore + sha256sum** | Daily backup and a restore drill (NFR12, RPO 24 h). | `ops/backup.sh`; `ops/restore.sh`; `eval/results/restore_drill.json` |

### C.5 Documentation, diagrams and demo

| Tool | What it does | Evidence |
|---|---|---|
| **Mermaid** + **Mermaid CLI** 12.0.0 (Puppeteer) | All architecture diagrams are text (`.mmd`) rendered to PNG/SVG, so diagrams are versioned with the code. | `docs/architecture.mmd`; `architecture/diagrams/*.mmd`; `architecture/diagrams/puppeteer-config.json`; `architecture/REPRODUCE.md` §5 |
| **OpenAPI 3** | Machine-readable API contract, committed and checked in CI. | `docs/openapi.v1.json` |
| **Playwright** (Chromium, Node.js) | Records the demo videos automatically from the running stack, with captions. | `perf/demo/record_demo.js`; `perf/demo/record_narrated.js` |
| **Pico TTS** | Synthetic narration (see Part A). | as above |

### C.6 Tools used in the architecture validation experiments (E1–E3)

The architecture deliverable (28 Oct 2026) measured its key decisions before implementation, with
**Docker** 29.9 / **Compose** 5.6, **PostgreSQL 16.15 + pgvector 0.8.7** (E1: 200,000 items, exact vs HNSW
recall and latency), **FastAPI + Celery + Redis** (E2: async pipeline and failure drills), **Locust**
(E2 read load), the `pgvector` Python adapter, NumPy and httpx, and an offline wheel folder so the E2 image
builds without internet. Every measured number maps to a file in `architecture/validation/MEASUREMENTS.md`,
with checksums in `SHA256SUMS`.

---

## Engineering practices these tools support

* **Reproducibility**: every dependency pinned; result files record commit, Python version and platform; the architecture package has a step-by-step `REPRODUCE.md` and SHA-256 checksums.
* **Shift-left security**: secret scan of full history, SAST (Bandit, Ruff `S`, CodeQL), dependency audit (pip-audit), container and IaC scan (Trivy), all blocking in CI; pre-commit hooks locally; an SBOM per build and signed provenance per release; automated dependency updates (Dependabot); non-root container; secrets only from `.env` (`.env.example` committed, `.env` git-ignored).
* **Contract-first API**: OpenAPI document committed and diffed in CI.
* **Reliability by design, tested**: idempotency keys, late acknowledgement, time limits, circuit breakers, budget reservation, each with a drill that proves it.
* **Observability**: the three pillars in one stack: metrics (Prometheus), alerts and dashboards (Grafana), and distributed traces (OpenTelemetry + Jaeger).
* **Honest AI evaluation**: the simulator is labelled everywhere; live-model quality (M5) is reported as not measured.

## Not used (listed so nobody assumes otherwise)

RabbitMQ (the broker is Redis), any cloud deployment, LLM evaluation frameworks such as promptfoo or ragas
(with no API key they would only grade the simulator), and any AI coding tool other than
Claude unless MRA adds one to the declaration.
