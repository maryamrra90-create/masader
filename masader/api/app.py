"""FastAPI backend (architecture B.3.1, C.2 IF-01..IF-08): requests with clarification, reports and their
exports, publication, corrections and review, API keys, health, readiness and metrics, plus the minimal
HTML form of the core slice."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import psycopg
import redis
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from psycopg_pool import PoolTimeout

from .. import config
from ..ids import canonical_hash, key_hash, new_api_key, new_id
from ..pipeline.kit import STAGES
from ..processing.redact import check_policy, redact
from ..redisclient import rds
from ..render.build import render_html
from ..settings import settings
from ..store.db import pool
from . import planner
from .models import Accepted, CorrectionIn, KeyIn, NeedsClarification, Question, ReportOut, RequestIn, ResolveIn, ReviewDecision

app = FastAPI(title="Masader API", version="1.0.0",
              description="Evidence-labelled research briefs (course implementation of architecture v1.2).")
HTTP_LAT = Histogram("masader_http_request_seconds", "API latency", ["route", "method", "status"],
                     buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
REQS = Counter("masader_requests_total", "Report requests by outcome", ["outcome"])
QUEUE_LEN = Gauge("masader_queue_length", "Celery queue length", ["queue"])
REPORTS = Gauge("masader_reports", "Reports by status", ["status"])
BUDGET_LEFT = Gauge("masader_budget_remaining_usd", "AI budget left in the period", ["purpose"])
UI = Path(__file__).parent / "ui.html"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers


def _err(status, code, message, trace_id, headers=None):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "trace_id": trace_id}},
                        headers=headers)


@app.middleware("http")
async def timing(request: Request, call_next):
    request.state.trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    t0 = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    HTTP_LAT.labels(getattr(route, "path", "other"), request.method, response.status_code).observe(time.perf_counter() - t0)
    response.headers["X-Trace-Id"] = request.state.trace_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(ApiError)
async def api_error(request, exc: ApiError):
    return _err(exc.status, exc.code, exc.message, request.state.trace_id, exc.headers)


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    msg = "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors())
    return _err(400, "invalid_request", msg or "invalid request", request.state.trace_id)


@app.exception_handler(PoolTimeout)
@app.exception_handler(psycopg.OperationalError)
async def db_down(request, exc):
    return _err(503, "database_unavailable", "the database is not reachable; retry later", request.state.trace_id)


@app.exception_handler(redis.exceptions.ConnectionError)
@app.exception_handler(redis.exceptions.TimeoutError)
async def queue_down(request, exc):
    return _err(503, "queue_unavailable", "the job queue is not reachable; nothing was queued", request.state.trace_id)


# ------------------------------------------------------------------------------------------------ auth
def caller(x_api_key: str | None = Header(default=None)) -> dict:
    if not x_api_key or len(x_api_key) < 20:
        raise ApiError(401, "unauthorized", "missing or invalid X-API-Key")
    with pool().connection() as c:
        row = c.execute("SELECT id, role::text AS role, rate_per_hour, key_hash, holder_name FROM api_keys "
                        "WHERE key_prefix=%s AND revoked_at IS NULL", (x_api_key[:8],)).fetchone()
    if row is None or not _same(bytes(row["key_hash"]), key_hash(x_api_key)):
        raise ApiError(401, "unauthorized", "missing or invalid X-API-Key")
    return row


def _same(a: bytes, b: bytes) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def need(key: dict, *roles: str) -> None:
    if key["role"] not in roles:
        raise ApiError(403, "forbidden", f"this action needs a {' or '.join(roles)} key")


def audit(c, actor, action: str, object_id: str | None, details: dict | None = None) -> None:
    c.execute("INSERT INTO audit_log (actor, action, object_id, details) VALUES (%s,%s,%s,%s)",
              (actor, action, object_id, json.dumps(details or {})))


def _rate_limit(key: dict) -> None:
    bucket = f"rl:{key['id']}:{int(time.time() // 3600)}"
    n = rds().incr(bucket)
    rds().expire(bucket, 3700)
    if n > key["rate_per_hour"]:
        rds().decr(bucket)
        REQS.labels("rate_limited").inc()
        raise ApiError(429, "rate_limited", f"limit of {key['rate_per_hour']} report requests per hour reached",
                       headers={"Retry-After": str(3600 - int(time.time()) % 3600)})


def _unrate(key: dict) -> None:
    rds().decr(f"rl:{key['id']}:{int(time.time() // 3600)}")


# ------------------------------------------------------------------------------------------------ admission
def _admit_capacity(c, key: dict) -> None:
    """Refuse a new report while ADMISSION_MAX_ACTIVE reports are queued, running or waiting. The advisory
    lock is held until the inserting transaction commits, so two requests cannot both take the last
    place. A refused request stores nothing and does not count toward the hourly rate."""
    if not settings.admission_max_active:
        return
    c.execute("SELECT pg_advisory_xact_lock(4242)")
    n = c.execute("SELECT count(*) AS n FROM reports WHERE status IN ('queued','running','waiting_provider')").fetchone()["n"]
    if n >= settings.admission_max_active:
        _unrate(key)
        REQS.labels("busy").inc()
        raise ApiError(429, "busy", f"{n} reports are in progress; retry after {settings.admission_retry_after_s} s",
                       headers={"Retry-After": str(settings.admission_retry_after_s)})


def _admit_budget(c, key: dict, purpose: str) -> None:
    ok = c.execute("SELECT 1 FROM budgets WHERE purpose=%s AND period_start=%s AND cap_usd - reserved_usd - spent_usd >= %s",
                   (purpose, settings.budget_period, settings.est_report_cost)).fetchone()
    if not ok:
        _unrate(key)
        REQS.labels("budget_exhausted").inc()
        raise ApiError(429, "budget_exhausted", f"the AI budget for {purpose} use is spent for this period")


def _initial_versions() -> dict:
    roles = config.routing()["roles"]
    prefix = "" if settings.model_mode == "live" else "sim:"
    return {"variant": config.routing().get("variant", "B0"), "release": config.active()["release"],
            **{r: f"{prefix}{v['primary']} / {v['prompt']}" for r, v in roles.items()}}


def _enqueue(c, request_id: str, req: dict, cache_key: bytes) -> str:
    report_id = new_id("rep_")
    c.execute("INSERT INTO reports (id, request_id, status, config_hash, versions, cache_key) VALUES (%s,%s,'queued',%s,%s,%s)",
              (report_id, request_id, config.config_hash(), json.dumps(_initial_versions()), cache_key))
    c.execute("INSERT INTO report_stages (report_id, stage) SELECT %s, unnest(%s::masader.stage_t[])", (report_id, STAGES))
    c.execute("UPDATE requests SET status='queued', updated_at=now() WHERE id=%s", (request_id,))
    return report_id


def _cache_key(req: dict) -> bytes:
    keep = {k: req.get(k) for k in ("topic", "questions", "from", "to", "geography", "entities", "sources")}
    return canonical_hash([keep, config.config_hash()])


def _cached(c, key: dict, cache_key: bytes) -> str | None:
    """A delivered report for the same request and configuration from the last 6 hours (own or published)."""
    r = c.execute("SELECT rep.id FROM reports rep JOIN requests q ON q.id=rep.request_id WHERE rep.cache_key=%s "
                  "AND rep.status='delivered' AND rep.publication IN ('private','published') "
                  "AND rep.created_at > now() - interval '6 hours' AND (q.api_key_id=%s OR rep.publication='published') "
                  "ORDER BY rep.created_at DESC LIMIT 1", (cache_key, key["id"])).fetchone()
    return r["id"] if r else None


def _start(report_id: str) -> None:
    from ..pipeline.tasks import start_pipeline
    start_pipeline(report_id)


# ------------------------------------------------------------------------------------------------ requests
@app.post("/v1/requests", status_code=202, response_model=Accepted | NeedsClarification,
          responses={200: {"model": NeedsClarification}, 409: {}, 422: {"description": "Unprocessable Content"}, 429: {}, 503: {}})
def create_request(body: RequestIn, key=Depends(caller), idempotency_key: str | None = Header(default=None)):
    if not idempotency_key or not (8 <= len(idempotency_key) <= 100):
        raise ApiError(400, "invalid_request", "Idempotency-Key header (8-100 characters) is required")
    if body.purpose != "live":
        need(key, "admin")                          # evaluation runs are charged to their own budget
    payload = json.loads(body.model_dump_json(by_alias=True))
    rhash = canonical_hash(payload)
    with pool().connection() as c:
        prior = c.execute("SELECT r.id, r.status::text AS status, r.request_hash, r.clarification, rep.id AS report_id "
                          "FROM requests r LEFT JOIN reports rep ON rep.request_id = r.id AND rep.version = 1 "
                          "WHERE r.api_key_id=%s AND r.idempotency_key=%s", (key["id"], idempotency_key)).fetchone()
    if prior:                                       # replay: same answer, nothing new queued
        if bytes(prior["request_hash"]) != rhash:
            raise ApiError(409, "idempotency_conflict", "Idempotency-Key was used with a different body")
        return _replay(prior)
    text = " \n".join([payload["topic"], *payload["questions"], *payload["entities"]])
    why = check_policy(text)
    if why:
        with pool().connection() as c:
            audit(c, key["id"], "request.rejected", None, {"reason": why})
        REQS.labels("rejected").inc()
        raise ApiError(422, "rejected_by_input_policy", why)
    redactions: list[str] = []
    for f in ("topic",):
        r = redact(payload[f])
        payload[f], redactions = r.text, redactions + r.found
    for f in ("questions", "entities"):
        out = []
        for s in payload[f]:
            r = redact(s)
            out.append(r.text)
            redactions += r.found
        payload[f] = out
    _rate_limit(key)
    plan, questions = planner.plan(payload, body.purpose)
    request_id = new_id("req_")
    status = "needs_clarification" if questions else "queued"
    try:
        with pool().connection() as c, c.transaction():
            c.execute("INSERT INTO requests (id, api_key_id, idempotency_key, request_hash, payload, redactions, status, "
                      "clarification, plan) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (request_id, key["id"], idempotency_key, rhash, json.dumps(payload), json.dumps(redactions), status,
                       json.dumps({"questions": questions}) if questions else None, json.dumps(plan)))
            audit(c, key["id"], "request.create", request_id, {"redactions": redactions, "status": status})
            if questions:
                REQS.labels("needs_clarification").inc()
                return JSONResponse(status_code=200, content=NeedsClarification(
                    request_id=request_id, status="needs_clarification", questions=[Question(**q) for q in questions],
                    answers_url=f"/v1/requests/{request_id}/answers").model_dump())
            ck = _cache_key(payload)
            hit = _cached(c, key, ck)
            if hit:
                c.execute("UPDATE requests SET status='delivered' WHERE id=%s", (request_id,))
                _unrate(key)
                REQS.labels("cache_hit").inc()
                return Accepted(request_id=request_id, report_id=hit, status="delivered",
                                status_url=f"/v1/reports/{hit}", cached=True)
            _admit_budget(c, key, body.purpose)
            _admit_capacity(c, key)
            report_id = _enqueue(c, request_id, payload, ck)
            rds().ping()                            # fail before commit if the queue is down
    except psycopg.errors.UniqueViolation:          # lost an idempotency race: return the winner
        with pool().connection() as c2:
            prior = c2.execute("SELECT r.id, r.status::text AS status, r.clarification, rep.id AS report_id FROM requests r "
                               "LEFT JOIN reports rep ON rep.request_id=r.id AND rep.version=1 "
                               "WHERE r.api_key_id=%s AND r.idempotency_key=%s", (key["id"], idempotency_key)).fetchone()
        return _replay(prior)
    _start(report_id)
    REQS.labels("queued").inc()
    return Accepted(request_id=request_id, report_id=report_id, status="queued", status_url=f"/v1/reports/{report_id}")


def _replay(prior):
    if prior["report_id"]:
        return Accepted(request_id=prior["id"], report_id=prior["report_id"], status="queued",
                        status_url=f"/v1/reports/{prior['report_id']}")
    qs = (prior["clarification"] or {}).get("questions", [])
    return JSONResponse(status_code=200, content={"request_id": prior["id"], "status": prior["status"],
                                                  "questions": qs, "answers_url": f"/v1/requests/{prior['id']}/answers"})


@app.post("/v1/requests/{request_id}/answers", status_code=202, response_model=Accepted)
def answer(request_id: str, answers: dict[str, str], key=Depends(caller)):
    with pool().connection() as c:
        with c.transaction():
            r = c.execute("SELECT * FROM requests WHERE id=%s AND api_key_id=%s FOR UPDATE", (request_id, key["id"])).fetchone()
            if r is None:
                raise ApiError(404, "not_found", "no such request for this key")
            if r["status"] != "needs_clarification":
                rep = c.execute("SELECT id FROM reports WHERE request_id=%s AND version=1", (request_id,)).fetchone()
                if rep:
                    return Accepted(request_id=request_id, report_id=rep["id"], status="queued",
                                    status_url=f"/v1/reports/{rep['id']}")
                raise ApiError(409, "conflict", f"request is {r['status']}")
            questions = r["clarification"]["questions"]
            qids = {q["id"] for q in questions}
            if set(answers) - qids:
                raise ApiError(400, "invalid_request", f"unknown question ids: {sorted(set(answers) - qids)}")
            for q in questions:
                v = answers.get(q["id"])
                if v is not None and v not in q["options"] and q["id"] != "narrow":
                    raise ApiError(400, "invalid_request", f"answer to {q['id']} must be one of {q['options']}")
                if v is not None and check_policy(v):
                    raise ApiError(422, "rejected_by_input_policy", check_policy(v))
            payload, full = planner.apply_answers(r["payload"], r["plan"] or {}, questions,
                                                  {k: redact(v).text for k, v in answers.items()})
            purpose = (r["plan"] or {}).get("purpose", "live")
            c.execute("UPDATE requests SET clarification=%s, payload=%s, updated_at=now() WHERE id=%s",
                      (json.dumps(dict(r["clarification"], answers=full)), json.dumps(payload), request_id))
            _rate_limit(key)
            _admit_budget(c, key, purpose)
            _admit_capacity(c, key)
            report_id = _enqueue(c, request_id, payload, _cache_key(payload))
            audit(c, key["id"], "request.answer", request_id, {"answers": full})
            rds().ping()
    _start(report_id)
    REQS.labels("queued").inc()
    return Accepted(request_id=request_id, report_id=report_id, status="queued", status_url=f"/v1/reports/{report_id}")


@app.get("/v1/requests/{request_id}")
def get_request(request_id: str, key=Depends(caller)):
    with pool().connection() as c:
        r = c.execute("SELECT id, status::text AS status, payload, clarification, redactions, created_at FROM requests "
                      "WHERE id=%s AND (api_key_id=%s OR %s IN ('reviewer','admin'))",
                      (request_id, key["id"], key["role"])).fetchone()
        if r is None:
            raise ApiError(404, "not_found", "no such request for this key")
        reps = c.execute("SELECT id, version, status::text AS status, publication::text AS publication FROM reports "
                         "WHERE request_id=%s ORDER BY version", (request_id,)).fetchall()
    return {**{k: r[k] for k in ("id", "status", "payload", "clarification", "redactions")},
            "created_at": r["created_at"].isoformat(), "reports": reps}


# ------------------------------------------------------------------------------------------------ reports
def _load_report(report_id: str, key: dict | None) -> dict:
    with pool().connection() as c:
        rep = c.execute("SELECT rep.*, rep.status::text AS status, rep.publication::text AS publication, req.api_key_id "
                        "FROM reports rep JOIN requests req ON req.id=rep.request_id WHERE rep.id=%s", (report_id,)).fetchone()
        if rep is None:
            raise ApiError(404, "not_found", "no such report")
        visible = rep["publication"] == "published" or (key is not None and (
            rep["api_key_id"] == key["id"] or key["role"] in ("reviewer", "admin")))
        if not visible:
            raise ApiError(404, "not_found", "no such report for this key")
        rep["stages"] = c.execute("SELECT stage::text, status::text, attempts FROM report_stages WHERE report_id=%s "
                                  "ORDER BY stage", (report_id,)).fetchall()
        nxt = c.execute("SELECT id FROM reports WHERE supersedes=%s", (report_id,)).fetchone()
        rep["superseded_by"] = nxt["id"] if nxt else None
    return rep


def _body(rep: dict) -> dict:
    """The stored body with the fields that change after delivery (publication, review) brought up to date."""
    body = dict(rep["body"] or {})
    body.update(report_id=rep["id"], request_id=rep["request_id"], status=rep["status"], publication=rep["publication"],
                version=rep["version"], review_status="reviewed" if rep["approved_at"] else "not_reviewed",
                change_note=rep["change_note"], supersedes=rep["supersedes"], superseded_by=rep.get("superseded_by"))
    return body


def _etag(rep: dict) -> str:
    h = hashlib.sha256(json.dumps([rep["status"], rep["publication"], str(rep["approved_at"]), rep["body"] is not None,
                                   [s["status"] for s in rep["stages"]]], default=str).encode()).hexdigest()[:20]
    return f'"{h}"'


def _export_ready(rep: dict) -> None:
    if rep["body"] is None:
        raise ApiError(409, "conflict", f"report is {rep['status']}; exports exist once it is delivered")


@app.get("/v1/reports/{report_id}.json")
def export_json(report_id: str, key=Depends(caller)):
    rep = _load_report(report_id, key)
    _export_ready(rep)
    return JSONResponse(_body(rep), headers={"Content-Disposition": f'attachment; filename="{report_id}-v{rep["version"]}.json"'})


@app.get("/v1/reports/{report_id}.html", response_class=HTMLResponse)
def export_html(report_id: str, key=Depends(caller)):
    rep = _load_report(report_id, key)
    _export_ready(rep)
    return HTMLResponse(render_html(_body(rep)), headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


# declared after the .json / .html routes, which would otherwise match {report_id}
@app.get("/v1/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: str, response: Response, key=Depends(caller), if_none_match: str | None = Header(default=None)):
    rep = _load_report(report_id, key)
    etag = _etag(rep)
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    b = _body(rep)
    return ReportOut(report_id=rep["id"], request_id=rep["request_id"], status=rep["status"], publication=rep["publication"],
                     version=rep["version"], review_status=b["review_status"], stages=rep["stages"], title=b.get("title"),
                     findings=b.get("findings", []), sections=b.get("sections", {}), statistics=b.get("statistics", []),
                     exports={"json": f"/v1/reports/{report_id}.json", "html": f"/v1/reports/{report_id}.html"},
                     versions={k: str(v) for k, v in (b.get("versions") or rep["versions"]).items()}
                     | {"config": rep["config_hash"]},
                     method=b.get("method", {}), notes=rep["notes"] or [], cost_usd=float(rep["cost_usd"]),
                     duration_s=rep["duration_s"], supersedes=rep["supersedes"], superseded_by=rep["superseded_by"])


@app.get("/r/{report_id}", response_class=HTMLResponse)
def public_report(report_id: str):
    """Published reports are readable without a key (Tier 4)."""
    with pool().connection() as c:
        rep = c.execute("SELECT rep.*, rep.status::text AS status, rep.publication::text AS publication FROM reports rep "
                        "WHERE id=%s", (report_id,)).fetchone()
    if rep is None or rep["publication"] not in ("published", "withdrawn", "superseded") or rep["body"] is None:
        raise ApiError(404, "not_found", "no such published report")
    if rep["publication"] == "withdrawn":
        return HTMLResponse(f"<!doctype html><title>Withdrawn</title><p>Report {report_id} was withdrawn.</p>", status_code=410)
    rep["superseded_by"] = None
    return HTMLResponse(render_html(_body(rep)), headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@app.post("/v1/reports/{report_id}/publish")
def publish(report_id: str, key=Depends(caller)):
    need(key, "reviewer", "admin")
    with pool().connection() as c, c.transaction():
        rep = c.execute("SELECT status::text AS status, publication::text AS publication FROM reports WHERE id=%s FOR UPDATE",
                        (report_id,)).fetchone()
        if rep is None:
            raise ApiError(404, "not_found", "no such report")
        if rep["status"] != "delivered":
            raise ApiError(409, "conflict", f"report is {rep['status']}, not delivered")
        if rep["publication"] in ("withdrawn", "superseded"):
            raise ApiError(409, "conflict", f"report is {rep['publication']}")
        if rep["publication"] != "published":
            c.execute("UPDATE reports SET publication='published', approver_key_id=%s, approved_at=now() WHERE id=%s",
                      (key["id"], report_id))
            audit(c, key["id"], "report.publish", report_id)
    return {"report_id": report_id, "publication": "published", "public_url": f"/r/{report_id}"}


@app.post("/v1/reports/{report_id}/withdraw")
def withdraw(report_id: str, decision: ReviewDecision | None = None, key=Depends(caller)):
    need(key, "reviewer", "admin")
    with pool().connection() as c, c.transaction():
        rep = c.execute("SELECT publication::text AS publication FROM reports WHERE id=%s FOR UPDATE", (report_id,)).fetchone()
        if rep is None:
            raise ApiError(404, "not_found", "no such report")
        if rep["publication"] not in ("published", "withdrawn"):
            raise ApiError(409, "conflict", f"report is {rep['publication']}; only a published report can be withdrawn")
        if rep["publication"] == "published":
            c.execute("UPDATE reports SET publication='withdrawn', change_note=%s WHERE id=%s",
                      (decision.note if decision else "withdrawn by a reviewer", report_id))
            audit(c, key["id"], "report.withdraw", report_id)
    return {"report_id": report_id, "publication": "withdrawn"}


# ------------------------------------------------------------------------------------------------ corrections and review
@app.post("/v1/reports/{report_id}/corrections", status_code=201)
def correction(report_id: str, body: CorrectionIn, key=Depends(caller)):
    rep = _load_report(report_id, key)
    with pool().connection() as c, c.transaction():
        if body.claim_id is not None and not c.execute("SELECT 1 FROM claims WHERE id=%s AND report_id=%s",
                                                       (body.claim_id, report_id)).fetchone():
            raise ApiError(400, "invalid_request", "claim_id does not belong to this report")
        dup = c.execute("SELECT id FROM corrections WHERE report_id=%s AND reported_by=%s AND claim_id IS NOT DISTINCT FROM %s "
                        "AND status='open'", (report_id, key["id"], body.claim_id)).fetchone()
        if dup:
            return {"correction_id": dup["id"], "status": "open", "duplicate": True}
        cid = c.execute("INSERT INTO corrections (report_id, claim_id, reported_by, reason) VALUES (%s,%s,%s,%s) RETURNING id",
                        (rep["id"], body.claim_id, key["id"], redact(body.reason).text)).fetchone()["id"]
        audit(c, key["id"], "correction.create", str(cid), {"report_id": report_id})
    return {"correction_id": cid, "status": "open"}


@app.get("/v1/review/queue")
def review_queue(key=Depends(caller)):
    need(key, "reviewer", "admin")
    with pool().connection() as c:
        reps = c.execute("SELECT id, request_id, status::text AS status, publication::text AS publication, notes, created_at "
                         "FROM reports WHERE status='in_review' OR (status='delivered' AND publication='private') "
                         "ORDER BY created_at DESC LIMIT 100").fetchall()
        cors = c.execute("SELECT id, report_id, claim_id, reason, created_at FROM corrections WHERE status='open' "
                         "ORDER BY created_at LIMIT 100").fetchall()
    return {"reports": reps, "corrections": cors}


@app.post("/v1/review/{report_id}")
def review_decision(report_id: str, body: ReviewDecision, key=Depends(caller)):
    """A reviewer clears a report the pipeline sent to review: approve -> delivered (marked reviewed), or
    reject -> failed. Only reports whose pipeline finished (render produced a body) can be approved."""
    need(key, "reviewer", "admin")
    with pool().connection() as c, c.transaction():
        rep = c.execute("SELECT status::text AS status, body IS NOT NULL AS has_body FROM reports WHERE id=%s FOR UPDATE",
                        (report_id,)).fetchone()
        if rep is None:
            raise ApiError(404, "not_found", "no such report")
        if rep["status"] != "in_review":
            raise ApiError(409, "conflict", f"report is {rep['status']}, not in review")
        if body.approve and not rep["has_body"]:
            raise ApiError(409, "conflict", "the pipeline stopped before the brief was written; reject it or re-request")
        status = "delivered" if body.approve else "failed"
        c.execute("UPDATE reports SET status=%s, approver_key_id=%s, approved_at=CASE WHEN %s THEN now() END, "
                  "notes = notes || %s::jsonb WHERE id=%s",
                  (status, key["id"], body.approve, json.dumps([f"review: {body.note}"]), report_id))
        c.execute("UPDATE requests SET status=%s WHERE id=(SELECT request_id FROM reports WHERE id=%s)", (status, report_id))
        audit(c, key["id"], "report.review", report_id, {"approve": body.approve})
    return {"report_id": report_id, "status": status}


@app.post("/v1/corrections/{correction_id}/resolve")
def resolve_correction(correction_id: int, body: ResolveIn, key=Depends(caller)):
    """Accepting a correction creates version n+1 of the report (the published version is immutable):
    the corrected finding gets the reviewer's text and is marked human-reviewed, or is removed with the
    sentences that cite it; the old version becomes superseded."""
    need(key, "reviewer", "admin")
    with pool().connection() as c, c.transaction():
        cor = c.execute("SELECT * FROM corrections WHERE id=%s FOR UPDATE", (correction_id,)).fetchone()
        if cor is None:
            raise ApiError(404, "not_found", "no such correction")
        if cor["status"] != "open":
            return {"correction_id": correction_id, "status": cor["status"]}
        new_id_ = None
        if body.accept:
            old = c.execute("SELECT *, publication::text AS publication FROM reports WHERE id=%s FOR UPDATE",
                            (cor["report_id"],)).fetchone()
            if old["body"] is None:
                raise ApiError(409, "conflict", "the report has no delivered body to correct")
            nb = json.loads(json.dumps(old["body"]))
            target = next((f for f in nb.get("findings", []) if f.get("claim_id") == cor["claim_id"]), None)
            if target and body.corrected_text:
                target["text"] = body.corrected_text
                target["reliability"]["reason"] += "; corrected by a reviewer"
                for sec in nb["sections"].values():
                    for s in sec:
                        if isinstance(s, dict) and target["id"] in s.get("findings", []):
                            s["text"] = body.corrected_text
            elif target:
                nb["findings"] = [f for f in nb["findings"] if f is not target]
                for k, sec in nb["sections"].items():
                    nb["sections"][k] = [s for s in sec if not (isinstance(s, dict) and target["id"] in s.get("findings", []))]
            nb.setdefault("notes", []).append(f"version {old['version'] + 1}: correction accepted: {body.note}")
            nxt = c.execute("SELECT coalesce(max(version),0)+1 AS v FROM reports WHERE request_id=%s",
                            (old["request_id"],)).fetchone()["v"]
            new_id_ = new_id("rep_")
            nb.update(report_id=new_id_, version=nxt)
            pub = "published" if old["publication"] == "published" else "private"
            c.execute("INSERT INTO reports (id, request_id, version, status, publication, variant, config_hash, versions, "
                      "cache_key, body, cost_usd, duration_s, notes, approver_key_id, approved_at, change_note, supersedes, stats) "
                      "VALUES (%s,%s,%s,'delivered',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s)",
                      (new_id_, old["request_id"], nxt, pub, old["variant"], old["config_hash"], json.dumps(old["versions"]),
                       old["cache_key"], json.dumps(nb), old["cost_usd"], old["duration_s"], json.dumps(nb["notes"]),
                       key["id"], body.note, old["id"], json.dumps(old["stats"])))
            c.execute("UPDATE reports SET publication='superseded' WHERE id=%s", (old["id"],))
            if cor["claim_id"]:
                c.execute("UPDATE claim_labels SET review_status='human_reviewed', reviewed_by=%s WHERE claim_id=%s",
                          (key["id"], cor["claim_id"]))
        c.execute("UPDATE corrections SET status=%s, resolved_at=now(), resolved_by=%s, resolution_note=%s WHERE id=%s",
                  ("accepted" if body.accept else "rejected", key["id"], body.note, correction_id))
        audit(c, key["id"], "correction.resolve", str(correction_id), {"accept": body.accept, "new_report": new_id_})
    return {"correction_id": correction_id, "status": "accepted" if body.accept else "rejected", "new_report_id": new_id_}


# ------------------------------------------------------------------------------------------------ keys
@app.get("/v1/me")
def me(key=Depends(caller)):
    return {"key_id": str(key["id"]), "role": key["role"], "holder_name": key["holder_name"],
            "rate_per_hour": key["rate_per_hour"]}


@app.post("/v1/keys", status_code=201)
def create_key(body: KeyIn, key=Depends(caller)):
    need(key, "admin")
    new = new_api_key(body.role)
    with pool().connection() as c, c.transaction():
        kid = c.execute("INSERT INTO api_keys (holder_name, holder_email, role, key_prefix, key_hash, rate_per_hour) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (body.holder_name, body.holder_email, body.role, new[:8],
                                                                    key_hash(new), body.rate_per_hour)).fetchone()["id"]
        audit(c, key["id"], "key.create", str(kid), {"role": body.role})
    return {"key_id": str(kid), "api_key": new, "note": "shown once; only its HMAC is stored"}


@app.delete("/v1/keys/{key_id}")
def revoke_key(key_id: str, key=Depends(caller)):
    need(key, "admin")
    with pool().connection() as c, c.transaction():
        n = c.execute("UPDATE api_keys SET revoked_at=now() WHERE id::text=%s AND revoked_at IS NULL", (key_id,)).rowcount
        audit(c, key["id"], "key.revoke", key_id)
    if not n:
        raise ApiError(404, "not_found", "no such active key")
    return {"key_id": key_id, "revoked": True}


# ------------------------------------------------------------------------------------------------ ops
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    checks = {}
    try:
        with pool().connection() as c:
            c.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["postgres"] = type(e).__name__
    try:
        rds().ping()
        checks["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["redis"] = type(e).__name__
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready", "checks": checks,
                                                                 "model_mode": settings.model_mode})


@app.get("/metrics")
def metrics():
    try:
        for q in ("llm", "cpu", "ingest"):
            QUEUE_LEN.labels(q).set(rds().llen(q))
    except redis.exceptions.RedisError:
        pass
    try:
        with pool().connection() as c:
            for r in c.execute("SELECT status::text AS s, count(*) AS n FROM reports GROUP BY 1"):
                REPORTS.labels(r["s"]).set(r["n"])
            for r in c.execute("SELECT purpose::text AS p, cap_usd - reserved_usd - spent_usd AS left FROM budgets "
                               "WHERE period_start=%s", (settings.budget_period,)):
                BUDGET_LEFT.labels(r["p"]).set(float(r["left"]))
    except (psycopg.Error, PoolTimeout):
        pass
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui():
    return HTMLResponse(UI.read_text(encoding="utf-8"))
