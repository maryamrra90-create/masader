# Demo video script (about 8 minutes)

Recorded by Mariam Alshamsi (MRA). Screen: a terminal on the left, a browser on the right. Say clearly at the
start, and leave visible on screen, that model outputs in this demo come from the **heuristic simulator** and
the news items are a **synthetic corpus**.

| Time | Show | Say |
|------|------|-----|
| 0:00–0:40 | Title slide: "Masader: evidence-labelled research briefs" and the architecture diagram (`docs/architecture.png`) | "Masader turns a question about recent events into a one-page brief in which every finding says how well it is supported and links to its sources. This is the implementation of my proposal and architecture. In this demo the AI model calls are simulated, because no API key was available, and the sources are a synthetic set of 37 news items. Everything else runs for real." |
| 0:40–1:30 | Terminal: `docker ps` (postgres, redis), then `make migrate seed`, `make run`, `make worker`, `make beat` in three panes | "The system is a FastAPI service, Celery workers for CPU and model stages, PostgreSQL with pgvector, and Redis. Seeding loads the source register, the budgets and the synthetic corpus." |
| 1:30–2:00 | `python -m masader.cli key create "Demo" demo@example.org admin`; point at "shown once" | "API keys are stored only as an HMAC; the key is shown once." |
| 2:00–3:00 | Browser at http://127.0.0.1:8000: paste key, keep the Hormuz example, press Request brief; the clarification question appears | "The planner checks the request first: ambiguous names, too broad, too little evidence, or asking for predictions. Here it asks one question with a default, so I can just send it." |
| 3:00–4:00 | Status line counting stages 1→8; then the brief in the sandboxed frame. Scroll: simulated banner, key findings with labels, a disputed finding, "insufficient" figure, what to watch | "Eight stages run in order. Each finding has a label: corroborated means at least two independent origins, here including an official statement. The insurance premium is disputed, 0.5 versus 0.7 percent, so the brief gives both versions. The old 6-hour reporting figure was superseded, so it is labelled insufficient and kept out of the findings." |
| 4:00–4:40 | Click a source link / show the evidence passage; show that the injection item (`newsroundup ... injection`) is not cited | "Every finding links to the exact passage. One corpus item contains a prompt injection that says traffic doubled; it never reaches the brief." |
| 4:40–5:20 | Terminal: `curl .../v1/reports/<id>` JSON, `versions` with `sim:` model names; `curl -X POST .../publish` with a reviewer key; open `/r/<id>` | "Reports are private until a reviewer publishes them. The JSON records every model and prompt version; in simulated mode the names start with sim." |
| 5:20–6:20 | Terminal: run `pytest -q tests/e2e -k killed` or show its log; then `eval/results/drills.json` summary | "Reliability: each stage takes a lease and commits fenced on it. In this test I kill a worker with SIGKILL in the middle of a stage; the reaper notices the expired lease and the report is still delivered exactly once. The drills also stop Redis and PostgreSQL and inject provider outages." |
| 6:20–7:30 | `eval/results` numbers on screen: tests passed, coverage, A15 20/20, dedup precision 1.0, seeded errors 73% vs 95% target, report time, load test p95 | "Here are the measured results against the targets. Some targets are met. The validator caught 73 percent of seeded errors, below the 95 percent target, because the support check here is a simple lexical simulator; with the real model this must be measured again. Arabic and English copies are not grouped because the offline embedder has no cross-lingual meaning." |
| 7:30–8:00 | `docs/deviations.md` and `docs/traceability.md` | "Every deviation from the architecture is listed with its reason, and the traceability matrix links each requirement to code, tests and results. Thank you." |

Checklist before recording: fresh `make seed`; create keys in advance and keep them off screen; close any
window that shows `.env`; zoom the terminal to 125%.
