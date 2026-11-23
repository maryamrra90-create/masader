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
| Claude (Anthropic), through Claude Code | MRA, for the implementation | Generated most of the source code, tests, evaluation scripts, CI/CD and deployment files, and documentation from the Proposal and Architecture v1.2, under MRA's direction. Ran the tests, evaluations, security scans and the Compose stack and reported the measured results. Added the engineering tooling (CodeQL, Dependabot, Trivy, pre-commit, release workflow, documentation site, OpenTelemetry tracing) through reviewed pull requests. Wrote the narration script for the demo video. | Every result in the report comes from a script in this repository that was actually run; raw outputs are in `eval/results/` with the commit id. 144 automated tests (unit, integration, AI-regression, end-to-end over real processes); CI on GitHub Actions passes on `main` (gitleaks, pre-commit, ruff, bandit, pip-audit, pytest with an 80% coverage floor, OpenAPI diff, image build, Trivy). Deviations are listed in `docs/deviations.md`. MRA reviews the deliverables. |
| Pico TTS (`pico2wave`, en-GB) | MRA, for the demo video | Synthetic voice reading the narration in `masader_demo_narrated.mp4`. It is not MRA's voice. | Transcript published in `demo/narration_transcript.md`. |
| **Other AI tools: ____________ (student to complete)** | MRA | _Any other AI tool actually used for the proposal, architecture, report or presentation (for example ChatGPT, Copilot, Gemini) and what it was used for. Write "None" if no other tool was used._ | |

## Statement on AI-generated content

* The Masader system itself uses AI models at run time (planner, extractor, verifier, writer, splitter,
  support check). In this build those calls are answered by a **deterministic heuristic simulator**; no
  output presented in the report or demo is a real model output, and this is labelled wherever it appears.
* No measurement in the report was invented or estimated without saying so. Cost figures are estimates at
  list prices on estimated token counts and are labelled as estimates.
* No credentials are included in the repository.
* The demo videos were recorded automatically from the running system with a script
  (`perf/demo/record_demo.js`, `perf/demo/record_narrated.js`). The narration in `masader_demo_narrated.mp4` is a
  **synthetic text-to-speech voice** (Pico TTS) reading a script written with Claude; it is not MRA's voice.

Name: Mariam Alshamsi (student ID 26021253)        Date: 19 November 2026

Signed: ____________________
