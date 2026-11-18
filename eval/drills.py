"""Failure drills (architecture H: recovery behaviour; proposal NFR3/NFR12 operational checks).

Part 1 runs on a real stack (Uvicorn API, Celery worker-llm/worker-cpu, beat) on the dev database and the
real PostgreSQL and Redis containers, with timers scaled down in the production order so recovery takes
seconds (see tests/e2e FAST):
  D1 provider down     both models of a role fail -> report waits (waiting_provider), resumes when they recover
  D2 invalid JSON      the extractor returns invalid JSON twice (call + repair) -> report goes to review
  D3 budget cap        the purpose budget cannot cover a report -> request refused before anything runs
  D4 Redis down        broker stopped -> /readyz 503, POST /v1/requests 503 and nothing stored; recovers on restart
  D5 PostgreSQL down   database stopped for 8 s while a report runs -> report still delivered after restart
  D6 worker killed     SIGKILL of worker-llm during a stage -> lease expires, reaper requeues, delivered once
Part 2 runs the in-process and end-to-end tests that cover the remaining drills (validator independence,
stage deadline, uncancellable call thread, stage that swallows the soft limit) and records their outcome.

Models are SIMULATED (fault injection through Redis keys); docker stop/start needs the containers named
mpg and mredis (make services). Run: python eval/drills.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from common import HORMUZ, ROOT, prepare, save  # noqa: I001
from report_time import Stack

FAST = {"LLM_TIMEOUT_S": "4", "LLM_MIN_CALL_S": "1", "STAGE_DEADLINE_MARGIN_S": "1", "STAGE_SOFT_LIMIT_S": "8",
        "STAGE_HARD_LIMIT_S": "10", "STAGE_LEASE_S": "12", "VISIBILITY_TIMEOUT_S": "14", "CANCEL_WAIT_S": "1.5",
        "STAGE_RETRY_S": "1", "PROVIDER_RETRY_S": "2", "REAPER_S": "3", "CB_OPEN_S": "3", "LLM_BACKOFF_S": "0.3", "LIVE_SOURCES": "0"}
PRIMARY, FALLBACK = "claude-sonnet-5-5", "claude-haiku-4-5"
BODY = dict(HORMUZ, sources={"prefer": ["official"]}, purpose="eval")      # no clarification question


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def wait_http(http, path, code, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if http.get(path).status_code == code:
                return True
        except Exception:  # noqa: BLE001 - the server may be restarting its connections
            pass
        time.sleep(0.5)
    return False


class Drills:
    def __init__(self, key):
        self.key = key
        self.h = {"X-API-Key": key}

    def submit(self, http, body=BODY):
        r = http.post("/v1/requests", json=dict(body, questions=[body["questions"][0] + f" [{uuid.uuid4().hex[:6]}]"]),
                      headers={**self.h, "Idempotency-Key": uuid.uuid4().hex})
        return r

    def wait(self, http, rid, statuses=("delivered", "in_review", "failed"), timeout=120):
        end, seen = time.time() + timeout, []
        while time.time() < end:
            try:
                rep = http.get(f"/v1/reports/{rid}", headers=self.h).json()
                if not seen or seen[-1] != rep["status"]:
                    seen.append(rep["status"])
                if rep["status"] in statuses:
                    return rep, seen
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        return None, seen


def part1() -> list[dict]:
    from masader.cli import create_key
    from masader.redisclient import rds
    from masader.store.db import pool, reset_pool
    prepare()
    _, key = create_key("drills", f"drills-{uuid.uuid4().hex[:6]}@example.org", "admin", rate=1000)
    d = Drills(key)
    out = []
    st = Stack({**FAST, "SIM_LATENCY_SCALE": "2"}).up()
    http = st.http
    try:
        # D1 provider down ------------------------------------------------------------------------------
        rds().set(f"fault:{PRIMARY}", "error")
        rds().set(f"fault:{FALLBACK}", "error")
        rid = d.submit(http).json()["report_id"]
        rep, seen = d.wait(http, rid, ("waiting_provider",), 60)
        waited = rep is not None
        time.sleep(4)
        rds().delete(f"fault:{PRIMARY}", f"fault:{FALLBACK}")
        t = time.time()
        rep, seen2 = d.wait(http, rid, timeout=120)
        out.append({"drill": "D1 provider down", "expected": "waiting_provider, then delivered after recovery",
                    "statuses": seen + seen2, "resume_to_done_s": round(time.time() - t, 1),
                    "passed": waited and rep is not None and rep["status"] == "delivered"})

        # D2 invalid JSON twice -----------------------------------------------------------------------------
        rds().set(f"fault:{PRIMARY}", "invalid_json")       # every call to the primary model returns broken JSON
        rid = d.submit(http).json()["report_id"]
        rep, seen = d.wait(http, rid)
        rds().delete(f"fault:{PRIMARY}")
        with pool().connection() as c:
            outs = [r["outcome"] for r in c.execute("SELECT outcome FROM ai_calls WHERE report_id=%s ORDER BY id", (rid,))]
        out.append({"drill": "D2 invalid JSON (call + repair)", "expected": "in_review after one repair attempt, never a brief built on bad output",
                    "statuses": seen, "call_outcomes": outs[:6],
                    "passed": rep is not None and rep["status"] == "in_review" and "invalid_json" in outs})

        # D3 budget cap ---------------------------------------------------------------------------------------
        from masader.settings import settings
        with pool().connection() as c:
            old = c.execute("SELECT cap_usd, reserved_usd, spent_usd FROM budgets WHERE purpose='eval' AND period_start=%s",
                            (settings.budget_period,)).fetchone()
            c.execute("UPDATE budgets SET spent_usd = cap_usd - reserved_usd - 0.01 WHERE purpose='eval' AND period_start=%s",
                      (settings.budget_period,))
            before = c.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
        r = d.submit(http)
        with pool().connection() as c:
            after = c.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
            c.execute("UPDATE budgets SET spent_usd=%s WHERE purpose='eval' AND period_start=%s",
                      (old["spent_usd"], settings.budget_period))
        out.append({"drill": "D3 budget cap", "expected": "429 budget_exhausted, nothing stored",
                    "http": r.status_code, "code": r.json().get("error", {}).get("code"), "reports_created": after - before,
                    "passed": r.status_code == 429 and r.json()["error"]["code"] == "budget_exhausted" and after == before})

        # D4 Redis down ---------------------------------------------------------------------------------------
        docker("stop", "mredis")
        not_ready = wait_http(http, "/readyz", 503, 20)
        with pool().connection() as c:
            before = c.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
        r = d.submit(http)
        with pool().connection() as c:
            after = c.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
        t = time.time()
        docker("start", "mredis")
        ready = wait_http(http, "/readyz", 200, 60)
        ready_s = round(time.time() - t, 1)
        time.sleep(5)                                   # workers reconnect to the broker
        rid = d.submit(http).json()["report_id"]
        rep, seen = d.wait(http, rid, timeout=120)
        out.append({"drill": "D4 Redis down", "expected": "readyz 503, request 503 queue_unavailable, nothing stored; after restart a new report is delivered",
                    "readyz_503": not_ready, "http": r.status_code, "code": r.json().get("error", {}).get("code"),
                    "reports_created_while_down": after - before, "ready_after_restart_s": ready_s,
                    "next_report": rep and rep["status"],
                    "passed": not_ready and r.status_code == 503 and after == before and ready and rep is not None
                    and rep["status"] == "delivered"})

        # D5 PostgreSQL down mid-report ----------------------------------------------------------------------
        rid = d.submit(http).json()["report_id"]
        d.wait(http, rid, ("running",), 30)
        time.sleep(2)
        docker("stop", "-t", "2", "mpg")
        not_ready = wait_http(http, "/readyz", 503, 20)
        time.sleep(8)
        t = time.time()
        docker("start", "mpg")
        reset_pool()
        ready = wait_http(http, "/readyz", 200, 90)
        ready_s = round(time.time() - t, 1)
        rep, seen = d.wait(http, rid, timeout=180)
        with pool().connection() as c:
            dup = c.execute("SELECT count(*) AS n FROM report_assertions WHERE report_id=%s GROUP BY assertion_no "
                            "HAVING count(*) > 1", (rid,)).fetchall()
        out.append({"drill": "D5 PostgreSQL down for ~8 s during a report", "expected": "readyz 503; after restart the report is delivered, no duplicate rows",
                    "readyz_503": not_ready, "ready_after_restart_s": ready_s, "statuses": seen,
                    "final": rep and rep["status"], "duplicate_assertion_rows": len(dup),
                    "passed": not_ready and ready and rep is not None and rep["status"] == "delivered" and not dup})

        # D6 worker-llm killed during a stage -----------------------------------------------------------------
        rid = d.submit(http).json()["report_id"]
        end = time.time() + 60
        while time.time() < end:
            stg = {x["stage"]: x["status"] for x in http.get(f"/v1/reports/{rid}", headers=d.h).json()["stages"]}
            if stg.get("extract") == "running":
                break
            time.sleep(0.2)
        p = st.procs.pop("worker-llm")
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(10)
        t = time.time()
        st.start("worker-llm2", [sys.executable, "-m", "celery", "-A", "masader.pipeline.celery_app", "worker", "-P", "prefork",
                                 "-c", "8", "-Q", "llm", "-n", f"llm2-{uuid.uuid4().hex[:4]}@%h"])
        rep, seen = d.wait(http, rid, timeout=150)
        with pool().connection() as c:
            started = c.execute("SELECT count(*) AS n FROM ai_calls WHERE report_id=%s AND outcome='started'", (rid,)).fetchone()["n"]
        att = {x["stage"]: x["attempts"] for x in rep["stages"]} if rep else {}
        out.append({"drill": "D6 worker-llm SIGKILL during extract", "expected": "delivered; extract attempted twice; no call left 'started'",
                    "kill_to_done_s": round(time.time() - t, 1), "extract_attempts": att.get("extract"),
                    "calls_left_started": started, "final": rep and rep["status"],
                    "passed": rep is not None and rep["status"] == "delivered" and att.get("extract", 0) >= 2 and started == 0})
    finally:
        rds().delete(f"fault:{PRIMARY}", f"fault:{FALLBACK}")
        docker("start", "mredis")
        docker("start", "mpg")
        st.down()
    return out


PART2 = {
    "validator independence (no model other than the writer's) -> review":
        "tests/integration/test_pipeline.py::test_validator_independence_sends_report_to_review",
    "stage deadline: retry once, then review": "tests/integration/test_pipeline.py::test_stage_deadline_retries_then_reviews",
    "budget never overspent under 20 concurrent reservations": "tests/integration/test_gateway.py::test_concurrent_reservations_never_overspend",
    "lost message: reaper restarts the report": "tests/integration/test_pipeline.py::test_reaper_restarts_a_report_whose_message_was_lost",
    "uncancellable call thread: child recycled": "tests/e2e/test_http_worker_flow.py::test_uncancellable_call_thread_recycles_the_child",
    "stage swallows soft limit: hard limit + reaper": "tests/e2e/test_http_worker_flow.py::test_stage_that_swallows_the_soft_limit_is_killed_and_reaped",
}


def part2() -> list[dict]:
    xml = os.path.join(os.environ.get("OBJECT_DIR", "/tmp"), "drills-junit.xml")
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *PART2.values()],
                   cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, check=False, capture_output=True)
    cases = {f"{c.get('classname')}::{c.get('name')}": c for c in ET.parse(xml).getroot().iter("testcase")}
    out = []
    for name, test in PART2.items():
        file, fn = test.split("::")
        cls = file.replace("/", ".").removesuffix(".py")
        c = cases.get(f"{cls}::{fn}")
        ok = c is not None and not any(ch.tag in ("failure", "error", "skipped") for ch in c)
        out.append({"drill": name, "test": test, "time_s": round(float(c.get("time")), 1) if c is not None else None, "passed": ok})
    return out


def main():
    p1 = part1()
    p2 = part2()
    out = {"what": "failure drills on real processes and containers; models SIMULATED with injected faults; timers scaled down",
           "part1_real_stack": p1, "part2_tests": p2, "passed": sum(x["passed"] for x in p1 + p2), "total": len(p1 + p2)}
    save("drills", out)
    for x in p1 + p2:
        print("PASS" if x["passed"] else "FAIL", x["drill"])
    return out


if __name__ == "__main__":
    main()
