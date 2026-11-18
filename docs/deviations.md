# Deviations from Architecture v1.2

Every place where the implementation differs from the Technical Architecture Deliverable v1.2 (28 Oct 2026)
or the Proposal (19 Oct 2026), with the reason and what it costs. "Simulated" or "substituted" items are
also labelled in the code, the API (`model_mode`, `sim:` model names) and every generated brief.

| ID | Area | Architecture v1.2 | Implementation | Why | Effect / what it costs |
|----|------|-------------------|----------------|-----|------------------------|
| D-01 | Model calls | Claude Sonnet 5.5 / Haiku 5.5 through the Anthropic SDK | `MODEL_MODE=simulated` (default): a deterministic heuristic simulator in `masader/simulated/heuristics.py` answers every role, reading the real stage payloads and returning schema-valid JSON. `MODEL_MODE=live` calls the Anthropic API with the same gateway. | No API key was available while building. | **All quality numbers measure the simulator, not Claude.** The live path is implemented and unit-tested against a fake SDK client, but has not been run against the real API. Model names are recorded as `sim:<model>`; briefs show a "simulated" banner. |
| D-02 | Embeddings | paraphrase-multilingual-MiniLM-L12-v2 (ONNX, fastembed) | `HashingEmbedder` (feature hashing, 384 dims) is the default; the fastembed class exists behind the same interface but is optional and untested | Model files could not be downloaded in the build environment | No cross-lingual similarity: Arabic and English versions of one story are not grouped by embedding (dedup recall 0.20 on cross-language pairs, 1.00 on same-language pairs). |
| D-03 | Source data | Live GDELT, ~20 RSS feeds, World Bank | Connectors for all three are implemented and unit-tested on recorded responses; evaluation uses a synthetic 37-item corpus with fictional "(fixture)" outlets and a 12-row World Bank fixture | No outbound access to the sources from the build environment; a fixed corpus is also needed for repeatable evaluation | Results describe the synthetic corpus. Live ingestion is off unless `LIVE_SOURCES=1`. |
| D-04 | Language detection | langdetect 1.0.9 | Script-share rule (Arabic vs Latin letters) | Only Arabic and English are in scope; avoids a dependency | Other languages are reported as `other`. |
| D-05 | PII redaction | Presidio pattern recognizers | Hand-written regular expressions for the same kinds (email, phone, IBAN, payment card, Emirates ID, passport number), plus policy patterns for credentials, confidentiality markings and profiling of private individuals | Presidio pulls spaCy into the image for regexes | Names and street addresses are not detected (as with Presidio's pattern-only mode). |
| D-06 | Article extraction | trafilatura 2.3 | Feed summaries / HTML stripped with the standard library | Dependency and network constraints | Full-page extraction is not available for live RSS items. |
| D-07 | Report schema | `sections` held plain strings (C.3) | Each section holds sentences `{text, findings[]}` that reference finding ids | The validator and the reader both need to know which finding a sentence rests on | API contract change from C.3; recorded in `docs/openapi.v1.json` and tested. |
| D-08 | Evidence rules | rules-1.1.0 | rules-1.1.1 | The reason text had a stray comma ("2 independent origins, (best tier A)") | Wording only; labels unchanged (A15 still 20/20). |
| D-09 | Migrations | Alembic | Plain numbered SQL files applied by `masader migrate` with a `schema_migrations` table | The schema is SQL-first (pgvector, CHECK constraints); one less framework | No auto-generated down migrations; rollback is restore-from-backup (as NFR12 already planned). |
| D-10 | Migration 0002 | — | Adds `sources.official`, correction resolution columns, `reports.stats`, `reports.trace_id`, `claims.item_id`, `requests.plan`, two indexes | Needed by the rules (official source), corrections workflow, and to keep the planner's plan with the request | Additive only. |
| D-11 | Near-duplicate test | MinHash LSH | Exact Jaccard over word shingles inside the request window | The window holds at most a few hundred items, so exact comparison is cheap and has no false negatives from hashing | Would need LSH for a much larger window. |
| D-12 | Token counting | Anthropic token-count endpoint | characters / 2 estimate for reservation and simulated cost | No API key | Cost figures are **estimates** at list prices on estimated tokens (about US$0.26 per report). Reservation stays conservative. |
| D-13 | Exports | Presigned S3 URLs | JSON/HTML exports are served by the API from the database/object store, with an ETag | Keeps the key check on every download; no S3 in the build environment | Slightly more API load per export (measured in the load test). |
| D-14 | Object store | S3 | Local directory store (default) and an S3 class behind the same interface (boto3; not exercised against AWS) | No AWS account | — |
| D-15 | Deployment | Docker Compose on one EC2 host, Route 53 health check | `compose.yaml` with the same services plus monitoring and edge profiles; image built locally; **not deployed to a cloud host** | No cloud account in scope for the build | NFR3 availability cannot be measured. |
| D-16 | CI/CD | GitHub Actions with gitleaks, image push | `.github/workflows/ci.yml` (lint, tests with coverage gate, OpenAPI diff, image build) is written but **has not run**: there was no GitHub repository | — | CI evidence is the same commands run locally (see `eval/results/` and the report). |
| D-17 | Metrics | Exporters on api, worker-llm (9101), worker-cpu (9102), node-exporter | Only the API exposes `/metrics` (HTTP latency, request outcomes, queue lengths, reports by status, budget left) | Worker exporters were not needed for the measured experiments | Per-stage timings come from `report_stages` in PostgreSQL instead of Prometheus. |
| D-18 | Experiment tracking | MLflow + DVC for frozen sets | Frozen test cases are versioned in Git (`eval/cases_e3.py`, `data/sample/`); results are JSON files in `eval/results/` with the commit id | Small data; no tracking server | No experiment UI. |
| D-19 | Stage 2 | React app, Arabic output, topic area B, watched topics | Not built (stage 2 by design) | Out of scope for this deliverable | The minimal HTML form at `/` is the only UI. |
| D-20 | Origin grouping | Attribution verbs list | Plain "said" added to the attribution verbs | Corpus copies attributed with "said" were not grouped | Slightly more attribution matches; precision stayed 1.0 on the corpus. |
| D-21 | Retrieval | Hybrid score, no floor | Hybrid score (0.5·cosine + 0.5·normalised ts_rank) with a cosine floor of 0.30 for items without a keyword match; stop words removed from the tsquery | Hashing-embedding noise between unrelated texts reaches about 0.2, and stop words matched every item | Tuned on the development corpus; must be re-checked with the real embedder. |
| D-22 | Claim merging | Merge near-identical claims | Merge only when the claims' numbers are equal | Two claims that differ only in a figure (6 vs 4 hours) were merged, hiding a superseded figure | Fewer merges; a changed figure stays visible as its own claim. |
| D-23 | Simulator limit | — | The simulated Writer leaves Arabic claims out of the brief | The simulator cannot translate | Arabic-only evidence does not appear in simulated briefs; a real model would translate it. |
| D-24 | Retention | Daily retention task (NFR9) | `masader.ops.retention` (daily beat task and `masader retention`) purges full text and raw objects after `sources.fulltext_days` and AI call traces after 90 days | As designed | Log retention is left to the Docker log driver. |

## Results that miss their targets

These are reported, not hidden; see the implementation report, Section 5.

* Seeded errors caught by the validator: target ≥ 95%. Measured with the **simulated** support check:
  60/82 = 73% (Wilson 95% CI 0.63–0.82) on errors generated from a pipeline report; 20/20 on the
  hand-written E3 set, which the heuristic was written against (optimistic).
* Clarification: 5 of 8 cases asked the expected questions; the three misses are the "widen" question,
  because the hit count matches generic words.
* Cross-language duplicate detection: recall 0.20 (hashing embedder, D-02).
