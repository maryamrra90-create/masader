"""End to end over real processes: the API under Uvicorn, a Celery worker (prefork) and beat (reaper), on
PostgreSQL and Redis. Timers are scaled down (same order as production) so recovery happens in seconds."""
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from tests.conftest import HORMUZ

pytestmark = [pytest.mark.db, pytest.mark.worker, pytest.mark.slow]
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAST = {"LLM_TIMEOUT_S": "4", "LLM_MIN_CALL_S": "1", "STAGE_DEADLINE_MARGIN_S": "1", "STAGE_SOFT_LIMIT_S": "8",
        "STAGE_HARD_LIMIT_S": "10", "STAGE_LEASE_S": "12", "VISIBILITY_TIMEOUT_S": "14", "CANCEL_WAIT_S": "1.5",
        "STAGE_RETRY_S": "1", "PROVIDER_RETRY_S": "2", "REAPER_S": "3", "CB_OPEN_S": "3"}


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Stack:
    def __init__(self, extra_env=None):
        self.env = {**os.environ, **FAST, **(extra_env or {}), "PYTHONPATH": ROOT}
        self.port = _port()
        self.procs = {}

    def start(self, name, args):
        log = open(os.path.join(os.environ["OBJECT_DIR"], f"{name}.log"), "w")
        self.procs[name] = subprocess.Popen(args, env=self.env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)

    def worker(self, name="worker"):
        self.start(name, [sys.executable, "-m", "celery", "-A", "masader.pipeline.celery_app", "worker", "-P", "prefork",
                          "-c", "2", "-Q", "cpu,llm", "-n", f"{name}-{uuid.uuid4().hex[:6]}@%h", "--loglevel", "INFO"])

    def up(self):
        self.start("api", [sys.executable, "-m", "uvicorn", "masader.api.app:app", "--port", str(self.port)])
        self.worker()
        self.start("beat", [sys.executable, "-m", "celery", "-A", "masader.pipeline.celery_app", "beat", "--loglevel", "INFO",
                            "-s", os.path.join(os.environ["OBJECT_DIR"], "beat-schedule")])
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30)
        for _ in range(100):
            try:
                if self.http.get("/readyz").status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("API did not start")

    def kill(self, name, sig=signal.SIGKILL):
        p = self.procs.pop(name)
        os.killpg(p.pid, sig)
        p.wait(10)

    def down(self):
        for name in list(self.procs):
            self.kill(name, signal.SIGTERM)


@pytest.fixture
def stack(db):
    s = Stack()
    yield s
    s.down()


def wait_status(http, key, rid, statuses, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        r = http.get(f"/v1/reports/{rid}", headers={"X-API-Key": key}).json()
        if r["status"] in statuses:
            return r
        time.sleep(0.5)
    raise AssertionError(f"report {rid} stayed {r['status']}: {r['stages']}")


def test_request_to_published_report_over_http(stack, keys):
    s = stack.up()
    h = {"X-API-Key": keys["requester"], "Idempotency-Key": uuid.uuid4().hex}
    r = s.http.post("/v1/requests", json=HORMUZ, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "needs_clarification"
    a = s.http.post(r.json()["answers_url"], json={"q1": "include"}, headers={"X-API-Key": keys["requester"]})
    assert a.status_code == 202
    rep = wait_status(s.http, keys["requester"], a.json()["report_id"], {"delivered", "in_review", "failed"})
    assert rep["status"] == "delivered" and rep["findings"]
    p = s.http.post(f"/v1/reports/{rep['report_id']}/publish", headers={"X-API-Key": keys["reviewer"]})
    assert p.status_code == 200
    assert "Findings and their evidence" in s.http.get(f"/r/{rep['report_id']}").text


def test_killed_worker_is_recovered_by_the_reaper(db, keys):
    """A worker is killed with SIGKILL while a stage runs (slow simulated calls); after a new worker
    starts, the expired lease is reaped and the report is delivered, with each stage committed once."""
    s = Stack({"SIM_LATENCY_SCALE": "2"}).up()          # extract / verify calls take about 3 s
    try:
        body = dict(HORMUZ, sources={"prefer": ["official"]})
        a = s.http.post("/v1/requests", json=body, headers={"X-API-Key": keys["requester"], "Idempotency-Key": uuid.uuid4().hex})
        rid = a.json()["report_id"]
        end = time.time() + 60
        while time.time() < end:
            st = {x["stage"]: x["status"] for x in s.http.get(f"/v1/reports/{rid}", headers={"X-API-Key": keys["requester"]}).json()["stages"]}
            if st.get("extract") == "running":
                break
            time.sleep(0.2)
        s.kill("worker")
        s.worker("worker2")
        rep = wait_status(s.http, keys["requester"], rid, {"delivered", "in_review", "failed"}, timeout=120)
        assert rep["status"] == "delivered"
        att = {x["stage"]: x["attempts"] for x in rep["stages"]}
        assert att["extract"] >= 2
        with db.connection() as c:
            n = c.execute("SELECT count(*) AS n FROM ai_calls WHERE report_id=%s AND outcome='started'", (rid,)).fetchone()["n"]
        assert n == 0
    finally:
        s.down()
