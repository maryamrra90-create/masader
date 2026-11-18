"""Report time and admission under a burst (NFR2: p95 report time under 10 minutes; architecture: at most
6 reports in progress, a 7th request gets 429 busy with Retry-After).

Real processes: the API under Uvicorn, worker-llm (prefork, 8 children, queue llm), worker-cpu (prefork,
3 children, queue cpu), and beat, on PostgreSQL and Redis, with the production timers.

Model latency is SIMULATED. Two settings:
  * SIM_LATENCY_SCALE=0  - no model latency: the time the system itself adds (queueing, database, rules).
  * SIM_LATENCY_SCALE=60 - every model call takes its full Appendix A6 time budget (+-20%), a pessimistic
                           stand-in for real model latency. Real Claude latency was NOT measured (no API key).

A burst of 10 requests is sent at once. Requests refused with 429 are resent after the Retry-After the
API returned. Time is measured per request from its first submission (including any wait for admission)
to delivered / in_review.
Run: python eval/report_time.py [scale ...]   (default: 0 60)
"""
from __future__ import annotations

import os
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid

import httpx
from common import HORMUZ, ROOT, prepare, save  # noqa: I001

N = 10


class Stack:
    def __init__(self, env: dict):
        self.env = {**os.environ, **env, "PYTHONPATH": str(ROOT)}
        self.procs: dict[str, subprocess.Popen] = {}
        self.port = 8765
        self.logdir = os.environ.get("OBJECT_DIR", "/tmp")

    def start(self, name, args):
        log = open(os.path.join(self.logdir, f"rt-{name}.log"), "w")
        self.procs[name] = subprocess.Popen(args, env=self.env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)

    def up(self):
        py = sys.executable
        self.start("api", [py, "-m", "uvicorn", "masader.api.app:app", "--port", str(self.port), "--workers", "2"])
        cel = [py, "-m", "celery", "-A", "masader.pipeline.celery_app"]
        self.start("worker-llm", cel + ["worker", "-P", "prefork", "-c", "8", "-Q", "llm", "-n", f"llm-{uuid.uuid4().hex[:4]}@%h"])
        self.start("worker-cpu", cel + ["worker", "-P", "prefork", "-c", "3", "-Q", "cpu,ingest", "-n", f"cpu-{uuid.uuid4().hex[:4]}@%h"])
        self.start("beat", cel + ["beat", "-s", os.path.join(self.logdir, "rt-beat-schedule")])
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30)
        for _ in range(150):
            try:
                if self.http.get("/readyz").status_code == 200:
                    time.sleep(3)          # let the workers connect
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("API did not start")

    def down(self):
        for p in self.procs.values():
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(20)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                os.killpg(p.pid, signal.SIGKILL)


def one(http, key, i, rec):
    h = {"X-API-Key": key}
    body = dict(HORMUZ, questions=[HORMUZ["questions"][0] + f" (burst request {i})"], purpose="eval")
    t0 = time.time()
    rec.update(i=i, refusals=0)
    def send(url, payload, headers):
        while True:            # a 429 (busy) is resent after the Retry-After the API gave
            r = http.post(url, json=payload, headers=headers)
            if r.status_code != 429:
                if r.status_code >= 400:
                    raise RuntimeError(f"{url}: {r.status_code} {r.text[:200]}")
                return r.json()
            rec["refusals"] += 1
            rec["retry_after_s"] = int(r.headers.get("Retry-After", "60"))
            time.sleep(rec["retry_after_s"])

    j = send("/v1/requests", body, {**h, "Idempotency-Key": uuid.uuid4().hex})
    if j.get("status") == "needs_clarification":
        rec["clarified"] = True
        j = send(j["answers_url"], {}, h)
    rec["admitted_after_s"] = round(time.time() - t0, 1)
    rid = j["report_id"]
    while True:
        rep = http.get(f"/v1/reports/{rid}", headers=h).json()
        if rep["status"] in ("delivered", "in_review", "failed"):
            break
        time.sleep(1)
    rec.update(report_id=rid, status=rep["status"], total_s=round(time.time() - t0, 1))


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def run(scale: float) -> dict:
    from masader.cli import create_key
    from masader.store.db import pool
    prepare()
    _, key = create_key("report-time eval", f"rt-{uuid.uuid4().hex[:6]}@example.org", "admin", rate=1000)
    st = Stack({"SIM_LATENCY_SCALE": str(scale), "LIVE_SOURCES": "0"}).up()
    try:
        recs = [{} for _ in range(N)]
        th = [threading.Thread(target=one, args=(st.http, key, i, recs[i])) for i in range(N)]
        t0 = time.time()
        for t in th:
            t.start()
        for t in th:
            t.join(3600)
        wall = time.time() - t0
    finally:
        st.down()
    ids = [r["report_id"] for r in recs if "report_id" in r]
    with pool().connection() as c:
        rows = c.execute("""SELECT stage, extract(epoch FROM finished_at - started_at) AS s FROM report_stages
                            WHERE report_id = ANY(%s) AND finished_at IS NOT NULL""", (ids,)).fetchall()
        proc = c.execute("SELECT id, duration_s FROM reports WHERE id = ANY(%s)", (ids,)).fetchall()
    per_stage: dict[str, list[float]] = {}
    for r in rows:
        per_stage.setdefault(r["stage"], []).append(float(r["s"]))
    totals = [r["total_s"] for r in recs if "total_s" in r]
    return {"sim_latency_scale": scale, "requests": N, "wall_s": round(wall, 1),
            "status_counts": {s: sum(r.get("status") == s for r in recs) for s in ("delivered", "in_review", "failed")},
            "refused_429_at_least_once": sum(r["refusals"] > 0 for r in recs),
            "total_s": {"p50": statistics.median(totals), "p95": p95(totals), "max": max(totals)} if totals else None,
            "processing_s_from_db": sorted(p["duration_s"] for p in proc if p["duration_s"] is not None),
            "stage_s_median": {k: round(statistics.median(v), 2) for k, v in per_stage.items()},
            "requests_detail": recs}


def main(scales: list[float]) -> dict:
    out = {"what": "report time and admission under a burst of 10; model latency SIMULATED", "target": "p95 < 600 s",
           "runs": [run(s) for s in scales]}
    save("report_time", out)
    for r in out["runs"]:
        print(r["sim_latency_scale"], r["total_s"], r["status_counts"], "refused:", r["refused_429_at_least_once"])
    return out


if __name__ == "__main__":
    main([float(a) for a in sys.argv[1:]] or [0.0, 60.0])
