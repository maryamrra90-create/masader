# Contribution and AI-usage declaration

**Project:** Masader, implementation deliverable
**Team:** individual project. Mariam Alshamsi (MRA) is the sole project member, manager and owner. The
people who will use the system are users, not contributors.

## Individual contributions

| Member | Role | Contribution |
|--------|------|--------------|
| Mariam Alshamsi (MRA) | Project owner, manager and sole member | Wrote the Proposal (19 Oct 2026) and the Technical Architecture v1.2 (28 Oct 2026) that this implementation follows; set the requirements, targets and scope; directed the implementation; reviews and accepts the deliverables; records the demo video. |

## AI tools used

| Tool | Used by | What it was used for | How the output was checked |
|------|---------|----------------------|----------------------------|
| Claude (Anthropic), through Claude Code | MRA, for the implementation | Generated most of the source code, tests, evaluation scripts, deployment files and documentation from the Proposal and Architecture, under MRA's direction. It also ran the tests and evaluations and reported the measured results. | Every result in the report comes from a script in this repository that was actually run; raw outputs are in `eval/results/` with the commit id. 139 automated tests (unit, integration, AI-regression, end-to-end over real processes); bandit and pip-audit scans clean (`eval/results/security_*.json`). Deviations are listed in `docs/deviations.md`. MRA reviews the deliverables. |
| _To be completed by MRA_ | MRA | _Any other AI tools used for the proposal, architecture or this deliverable (for example ChatGPT, Copilot, Gemini, Manus) and their purpose_ | |

## Statement on AI-generated content

* The Masader system itself uses AI models at run time (planner, extractor, verifier, writer, splitter,
  support check). In this build those calls are answered by a **deterministic heuristic simulator**; no
  output presented in the report or demo is a real model output, and this is labelled wherever it appears.
* No measurement in the report was invented or estimated without saying so. Cost figures are estimates at
  list prices on estimated token counts and are labelled as estimates.
* No credentials are included in the repository.

Signed: ____________________  (Mariam Alshamsi)        Date: ____________
