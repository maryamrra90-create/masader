# Traceability matrix

Status values: **Completed** (built as designed and tested), **Modified** (built differently from v1.2; the
deviation id says why), **Partial** (built, but part of it is missing or not exercised), **Simulated** (the
code path exists; the evidence comes from a stand-in, not the real service), **Not built** (stage 2 or out
of scope). Evidence points to code, tests (`tests/...::name`) and measured results (`eval/results/*.json`).

## Functional requirements

| ID | Requirement | Component | Status | Evidence |
|----|-------------|-----------|--------|----------|
| FR1 | Submit a one-off request with an API key | `masader/api/app.py` POST /v1/requests | Completed | `test_api.py::test_auth`, `::test_validation_errors`, `::test_idempotency`; e2e `test_request_to_published_report_over_http` |
| FR2 | Detect broad, ambiguous, unsupported or low-evidence requests; up to 3 questions | `masader/api/planner.py` (rules + planner role) | Partial | `test_api.py::test_planner_rules`, `::test_clarification_flow_and_answer_validation`, `::test_too_little_evidence_offers_to_widen`, `::test_planner_model_failure_falls_back_to_rules`; `eval/results/quality.json` clarification 5/8 (the "widen" rule counts keyword hits, so generic words hide low evidence) |
| FR3 | Collect from GDELT, RSS, World Bank under the source register | `masader/connectors/*`, `processing/ingest.py`, `config/sources.yaml` | Partial / Simulated | Connectors unit-tested on recorded responses (`test_connectors_settings_render.py`); live endpoints not reached (D-03); evaluation uses the synthetic corpus |
| FR4 | Four data tiers with provenance | `store/migrations/0001_*.sql`, `0002_*.sql` | Completed (Modified: D-10) | `test_ingest_cli.py::test_seeded_corpus_is_grouped`; claim → passage → item → source joins used by every brief |
| FR5 | Exact and near duplicates, origin groups | `processing/origin_groups.py`, stage dedup | Completed (Modified: D-02, D-11, D-20) | `test_dedup_corpus.py`; `quality.json`: pairwise precision 1.00, recall 1.00 same-language, 0.20 cross-language |
| FR6 | Extract claims with type, passage, source | stage extract, extractor prompt | Simulated | `test_pipeline.py::test_full_pipeline_delivers_a_labelled_brief`; passages are checked to be verbatim (`test_simulator_and_schemas.py`) |
| FR7 | Evidence labels with reliability | `rules/evidence_rules.py` rules-1.1.1, stage label | Completed (Modified: D-08) | `test_rules_a15.py` (20/20 A15 cases); `quality.json` a15 20/20 |
| FR8 | One-page English brief, facts apart from analysis | stage write, `render/templates/brief.en.html.j2` | Simulated (Modified: D-07, D-23) | `test_simulator_and_schemas.py::test_writer_follows_the_label_rules`; rendered HTML escapes source text (`test_html_render_escapes_source_text`) |
| FR9 | Validate every checkable assertion | stage validate: `rules.check_assertion` + support check (independent model) | Partial / Simulated | `test_rules_a15.py`, `test_pipeline.py::test_validator_independence_sends_report_to_review`; `seeded_errors.json`: 60/82 (73%) caught, target 95% **not met** with the simulated support check |
| FR10 | Private delivery, reviewer publication, corrections as versions | publish / withdraw / corrections / review routes | Completed | `test_api.py::test_publish_withdraw_and_public_page`, `::test_published_report_is_immutable`, `::test_correction_creates_a_new_version`, `::test_review_decision_for_in_review_report` |
| FR11 | JSON and HTML | stage render, `.json` / `.html` routes | Completed (Modified: D-13) | `test_api.py::test_report_access_etag_exports_and_cache` |
| FR13 | AI label and versions with each output | `reports.versions`, ReportOut.versions, HTML footer and simulated banner | Completed | `test_pipeline.py::test_full_pipeline_delivers_a_labelled_brief` (versions, `sim:` names) |
| FR14 | Statement-type classifier | — | Not built (stage 2) | — |

## Non-functional requirements

| ID | Target | Status | Evidence |
|----|--------|--------|----------|
| NFR1 | GET report/status p95 < 2 s with 50 users | See `eval/results/load_nfr1.json` | `perf/locustfile.py`, `perf/run_load.py` (local host, same machine as Locust) |
| NFR2 | Report p95 < 10 min (≤ 6 in progress) | Simulated | `eval/results/report_time.json`: burst of 10 on real workers; model latency simulated (D-01) |
| NFR3 | Availability ≥ 99% | Not measured | Not deployed to a host (D-15); health checks in `compose.yaml`, alert rules in `ops/prometheus/alerts.yml` |
| NFR4 | Every /v1 route needs a key; roles checked | Completed | `test_api.py::test_auth`, `::test_eval_purpose_needs_admin`; `test_openapi_contract.py::test_every_v1_route_needs_an_api_key_except_public_pages` |
| NFR5 | Redaction before any external AI call; no secrets in Git | Completed (Modified: D-05) | `test_text_and_redact.py::test_redaction_of_personal_data`, `::test_input_policy`; `test_api.py::test_input_policy_and_redaction`; `.env` ignored, `.env.example` has no values; gitleaks not run (D-16) |
| NFR6 | AI ≤ US$200/semester; caps never exceeded | Completed (spend estimated) | `test_gateway.py::test_budget_cap_refuses_the_call_before_it_is_made`, `::test_concurrent_reservations_never_overspend`; drill D3; cost per report is an estimate (D-12) |
| NFR8 | Clean checkout to running stack < 30 min | Partial | `README.md`, `Makefile`, `compose.yaml`; image build measured (see report); not timed with a classmate |
| NFR9 | Raw text ≤ 30 days, traces 90 days, deletion within 24 h | Completed | `pipeline/retention.py`, `test_ingest_cli.py::test_retention_purges_old_full_text_and_traces_but_keeps_excerpts`; `masader delete-user` and `test_api.py::test_delete_user_data` |
| NFR10 | Container memory ≤ 6.2 GiB | Not measured | Compose limits not set in this build |
| NFR11 | 255,500 items, search p95 < 500 ms without ANN | Not re-measured | Exact pgvector scan as designed; measured in the architecture's E1, not repeated here |
| NFR12 | RPO 24 h, RTO 4 h | Partial | `ops/backup.sh`, `ops/restore.sh`; restore drill result in `eval/results/drills.json` (local) |

## Architecture components

| Component (v1.2) | Status | Evidence |
|------------------|--------|----------|
| api (FastAPI, IF-01..IF-08) | Completed | `masader/api/`, `docs/openapi.v1.json`, 22 API integration tests |
| worker-cpu / worker-llm (Celery prefork, queues cpu / llm / ingest) | Completed | `pipeline/celery_app.py`; e2e tests and `report_time.py` run them as separate processes |
| Stage protocol: atomic claim, fenced commit, outcomes | Completed | `test_pipeline.py::test_stage_claim_is_exclusive_and_ordered`, `::test_fenced_commit_refuses_a_lost_lease`, `::test_redelivered_stage_is_not_run_twice` |
| Reaper (expired leases, lost calls, stuck reports) | Completed | `test_pipeline.py::test_reaper_recovers_expired_leases_and_lost_calls`, `::test_reaper_restarts_a_report_whose_message_was_lost`; e2e worker kill |
| Gateway: reservation, retry, repair, fallback, breaker, cancel, independence | Completed (provider simulated) | 12 tests in `test_gateway.py` |
| Child recycling for stuck call threads | Completed | e2e `test_uncancellable_call_thread_recycles_the_child`, `test_stage_that_swallows_the_soft_limit_is_killed_and_reaped` |
| Anthropic provider (live) | Partial | `gateway/providers.py::AnthropicProvider`; request shape, error mapping, cancel tested with a fake client (`test_providers.py`); never called live |
| Embeddings (MiniLM) | Modified (D-02) | `processing/embed.py` |
| Retrieval (hybrid FTS + vector) | Modified (D-21) | `test_ingest_cli.py::test_retrieval_is_windowed_and_capped` |
| Object store | Modified (D-14) | `store/objectstore.py` |
| Migrations | Modified (D-09) | `store/db.py::migrate` |
| Scheduler (beat): reaper, retention, live ingestion | Completed (live ingestion off by default) | `pipeline/celery_app.py` |
| Monitoring: Prometheus, Grafana, alerts | Partial (D-17) | `ops/prometheus/`, `ops/grafana/`; API metrics tested in `test_api.py::test_ops_endpoints` |
| Edge: Caddy TLS | Partial | `ops/caddy/Caddyfile`, compose `edge` profile; not run with a public domain |
| Docker image and Compose | Completed (local) | `Dockerfile`, `compose.yaml`; build and stack run recorded in the report |
| CI/CD | Partial (D-16) | `.github/workflows/ci.yml`, written, not run |
| Experiment tracking (MLflow, DVC) | Modified (D-18) | `eval/`, `eval/results/` |
| React app, Arabic output, topic area B, watched topics | Not built (stage 2, D-19) | — |
