"""Test configuration.

Unit tests need nothing. Tests marked `db` need PostgreSQL 16 with pgvector and Redis: by default
postgresql://masader:masader@localhost:5432 and redis://localhost:6379 (start them with
`docker compose up -d postgres redis` or `make services`). They use a separate database, masader_test,
created fresh for each test session, and Redis database 15.

Settings are read when masader is first imported, so the test environment is set here before that.
"""
from __future__ import annotations

import os
import tempfile

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL", "postgresql://masader:masader@localhost:5432/postgres")
TEST_DB = "masader_test"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
os.environ["OBJECT_DIR"] = tempfile.mkdtemp(prefix="masader-objects-")
os.environ.setdefault("LLM_BACKOFF_S", "0.01")
os.environ.setdefault("MODEL_MODE", "simulated")
os.environ.setdefault("SIM_LATENCY_SCALE", "0")
os.environ.setdefault("ADMISSION_MAX_ACTIVE", "6")

import uuid  # noqa: E402

import pytest  # noqa: E402

_DB_OK: bool | None = None


def _services_up() -> bool:
    global _DB_OK
    if _DB_OK is None:
        try:
            import psycopg
            import redis

            with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=2) as c:
                c.execute("SELECT 1")
            redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=1).ping()
            _DB_OK = True
        except Exception:  # noqa: BLE001
            _DB_OK = False
    return _DB_OK


def pytest_collection_modifyitems(config, items):
    if _services_up():
        return
    skip = pytest.mark.skip(reason="PostgreSQL/pgvector and Redis are not reachable (see tests/conftest.py)")
    for it in items:
        if "db" in it.keywords or "worker" in it.keywords:
            it.add_marker(skip)


@pytest.fixture(scope="session")
def database():
    """Fresh masader_test database: migrations, budgets, source register and the synthetic corpus."""
    import psycopg

    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEST_DB}")
    from masader import cli
    from masader.store.db import migrate

    migrate()
    out = cli.seed(fixtures=True)
    yield out
    from masader.store.db import reset_pool
    reset_pool()


TRANSIENT = ("corrections", "report_assertions", "claim_labels", "claim_evidence", "claim_passages", "claims",
             "report_stages", "ai_calls", "audit_log")


@pytest.fixture
def db(database):
    """Each test starts with no requests or reports, full budgets and an empty Redis database."""
    from masader.redisclient import rds
    from masader.settings import settings
    from masader.store.db import pool

    rds().flushdb()
    with pool().connection() as c:
        c.execute("TRUNCATE " + ", ".join(TRANSIENT) + " CASCADE")
        c.execute("UPDATE reports SET supersedes=NULL")
        c.execute("DELETE FROM reports")
        c.execute("DELETE FROM requests")
        c.execute("DELETE FROM api_keys")
        c.execute("UPDATE budgets SET reserved_usd=0, spent_usd=0 WHERE period_start=%s", (settings.budget_period,))
    yield pool()
    rds().flushdb()


@pytest.fixture
def keys(db):
    from masader.cli import create_key

    out = {}
    for role in ("requester", "reviewer", "admin"):
        _, k = create_key(f"Test {role}", f"{role}@example.org", role, rate=50)
        out[role] = k
    _, out["other"] = create_key("Other requester", "other@example.org", "requester", rate=50)
    return out


@pytest.fixture
def started(monkeypatch):
    """Records the reports the API would hand to Celery instead of sending them."""
    from masader.api import app as appmod

    ids: list[str] = []
    monkeypatch.setattr(appmod, "_start", lambda rid: ids.append(rid))
    return ids


@pytest.fixture
def client(db, started):
    from fastapi.testclient import TestClient

    from masader.api.app import app

    return TestClient(app)


HORMUZ = {"topic": "Commercial shipping through the Strait of Hormuz",
          "questions": ["What has happened to commercial shipping in the last 7 days, and how have Gulf and Indian officials responded?"],
          "from": "2026-10-12", "to": "2026-10-19", "geography": ["OMN", "ARE", "IND"]}


def idem() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


def run_pipeline(report_id: str) -> list[str]:
    """Runs the eight stages in this process, in order (what the Celery chain does across workers)."""
    from masader.pipeline import tasks
    from masader.pipeline.kit import STAGES, execute_stage

    return [execute_stage(s, report_id, tasks.FUNCS[s]) for s in STAGES]


def request_report(client, key: str, body: dict | None = None, answers: dict | None = None) -> str:
    """POST a request (answering any clarification with the defaults or `answers`); returns the report id."""
    r = client.post("/v1/requests", json=body or HORMUZ, headers={"X-API-Key": key, **idem()})
    assert r.status_code in (200, 202), r.text
    if r.json()["status"] == "needs_clarification":
        r = client.post(r.json()["answers_url"], json=answers or {}, headers={"X-API-Key": key})
        assert r.status_code == 202, r.text
    return r.json()["report_id"]
