"""Helpers for the evaluation scripts: a database with the synthetic corpus, an admin key, and running a
report through the eight stages in this process. Every script writes its raw results to eval/results/."""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
sys.path.insert(0, str(ROOT))

HORMUZ = {"topic": "Commercial shipping through the Strait of Hormuz",
          "questions": ["What has happened to commercial shipping in the last 7 days, and how have Gulf and Indian officials responded?"],
          "from": "2026-10-12", "to": "2026-10-19", "geography": ["OMN", "ARE", "IND"]}


def environment() -> dict:
    from masader.settings import settings
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = None
    return {"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "git_commit": commit,
            "python": platform.python_version(), "platform": platform.platform(), "cpus": os.cpu_count(),
            "model_mode": settings.model_mode, "embedder": settings.embedder}


def save(name: str, data: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"{name}.json"
    p.write_text(json.dumps({"environment": environment(), **data}, indent=1, ensure_ascii=False, default=str) + "\n")
    print(f"wrote {p.relative_to(ROOT)}")
    return p


def prepare() -> None:
    from masader import cli
    from masader.store.db import migrate
    migrate()
    cli.seed(fixtures=True)


def client_and_key(role: str = "admin"):
    from unittest import mock

    from fastapi.testclient import TestClient

    from masader.api import app as appmod
    from masader.cli import create_key

    mock.patch.object(appmod, "_start", lambda rid: None).start()
    _, key = create_key(f"eval {role}", f"eval-{uuid.uuid4().hex[:6]}@example.org", role, rate=1000)
    return TestClient(appmod.app), key


def run_report(client, key: str, body: dict, answers: dict | None = None) -> str:
    from masader.pipeline import tasks
    from masader.pipeline.kit import STAGES, execute_stage

    r = client.post("/v1/requests", json=body, headers={"X-API-Key": key, "Idempotency-Key": uuid.uuid4().hex})
    if r.json().get("status") == "needs_clarification":
        r = client.post(r.json()["answers_url"], json=answers or {}, headers={"X-API-Key": key})
    rid = r.json()["report_id"]
    for s in STAGES:
        execute_stage(s, rid, tasks.FUNCS[s])
    return rid
