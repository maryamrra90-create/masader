"""Stage machinery shared by the eight pipeline stages (architecture B.3.4, ported from experiment E2).

Each stage runs only after an atomic claim of its report_stages row (a lease owned by one execution) and
commits its rows in the same transaction as a fenced UPDATE that succeeds only if this execution still
owns the lease, so duplicate or redelivered messages can neither run a stage twice at once nor commit
twice. `execute_stage` returns an Outcome; the Celery task in tasks.py maps it to retry / ignore / next.
"""
from __future__ import annotations

import concurrent.futures as cf
import contextvars
import datetime as dt
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded

from ..gateway import client as gw
from ..gateway.errors import BudgetExhausted, CallCancelled, InvalidOutput, NoIndependentModel, ProviderUnavailable, StageDeadline
from ..redisclient import rds
from ..settings import settings
from ..store.db import pool

log = logging.getLogger("masader.pipeline")
STAGES = ["collect", "dedup", "extract", "verify", "label", "write", "validate", "render"]


class Recycle(Exception):
    """A call thread did not stop after cancellation; the child process must be replaced."""


@dataclass
class Ctx:
    report_id: str
    request: dict                        # validated RequestIn (redacted), with "from"/"to" keys
    plan: dict                           # planner output stored with the request
    answers: dict
    purpose: str
    owner: str
    started: float
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)     # role -> recorded model of the last call
    prompts: dict[str, str] = field(default_factory=dict)

    def calls(self) -> gw.StageCalls:
        return gw.StageCalls(deadline=self.started + settings.stage_soft_limit_s - settings.stage_deadline_margin_s)

    def output(self, stage: str) -> dict:
        with pool().connection() as c:
            r = c.execute("SELECT output_ref FROM report_stages WHERE report_id=%s AND stage=%s",
                          (self.report_id, stage)).fetchone()
        return json.loads(r["output_ref"]) if r and r["output_ref"] else {}


@dataclass
class StageResult:
    output: dict                                      # stored as report_stages.output_ref (JSON)
    write: Callable[[Any], None] | None = None        # writes the stage's rows inside the fenced commit


# --------------------------------------------------------------------------------------------- claim / commit
def claim(report_id: str, stage: str, owner: str) -> str:
    """Atomically take the stage: one UPDATE that matches only a pending stage (or one whose lease expired)
    whose previous stage is done. Returns 'claimed', 'done' or 'busy'."""
    prev = STAGES[STAGES.index(stage) - 1] if stage != STAGES[0] else None
    with pool().connection() as c:
        r = c.execute(
            "UPDATE report_stages s SET status='running', lease_owner=%s, lease_until=now() + make_interval(secs => %s), "
            "attempts=attempts+1, started_at=now(), error=NULL "
            "WHERE report_id=%s AND stage=%s AND (status='pending' OR (status='running' AND lease_until < now())) "
            "AND (%s::masader.stage_t IS NULL OR EXISTS (SELECT 1 FROM report_stages p WHERE p.report_id=s.report_id "
            "AND p.stage=%s::masader.stage_t AND p.status='done')) RETURNING attempts",
            (owner, settings.stage_lease_s, report_id, stage, prev, prev)).fetchone()
        if r:
            return "claimed"
        st = c.execute("SELECT status FROM report_stages WHERE report_id=%s AND stage=%s", (report_id, stage)).fetchone()
    return "done" if st and st["status"] == "done" else "busy"


def commit(ctx: Ctx, stage: str, result: StageResult) -> bool:
    """Fenced commit: the stage's rows and its 'done' status in one transaction, only if this execution
    still owns the lease."""
    with pool().connection() as c, c.transaction():
        n = c.execute("UPDATE report_stages SET status='done', finished_at=now(), output_ref=%s, lease_owner=NULL, "
                      "lease_until=NULL WHERE report_id=%s AND stage=%s AND status='running' AND lease_owner=%s",
                      (json.dumps(result.output, ensure_ascii=False, default=str), ctx.report_id, stage, ctx.owner)).rowcount
        if n != 1:
            return False
        if result.write:
            result.write(c)
        if ctx.notes:
            c.execute("UPDATE reports SET notes = notes || %s::jsonb WHERE id=%s", (json.dumps(ctx.notes), ctx.report_id))
        if ctx.stats:
            c.execute("UPDATE reports SET stats = stats || %s::jsonb WHERE id=%s", (json.dumps(ctx.stats), ctx.report_id))
        if ctx.models:
            v = {role: f"{m} / {ctx.prompts.get(role, '')}" for role, m in ctx.models.items()}
            c.execute("UPDATE reports SET versions = versions || %s::jsonb WHERE id=%s", (json.dumps(v), ctx.report_id))
    return True


def release(report_id: str, stage: str, owner: str, status: str, error: str) -> None:
    with pool().connection() as c:
        c.execute("UPDATE report_stages SET status=%s, error=%s, lease_owner=NULL, lease_until=NULL, finished_at=now() "
                  "WHERE report_id=%s AND stage=%s AND lease_owner=%s", (status, error[:500], report_id, stage, owner))


def set_report(report_id: str, status: str) -> None:
    with pool().connection() as c, c.transaction():
        c.execute("UPDATE reports SET status=%s WHERE id=%s AND status NOT IN ('delivered', 'failed')", (status, report_id))
        c.execute("UPDATE requests SET status=%s, updated_at=now() WHERE id=(SELECT request_id FROM reports WHERE id=%s)",
                  (status, report_id))


def add_note(report_id: str, note: str) -> None:
    with pool().connection() as c:
        c.execute("UPDATE reports SET notes = notes || %s::jsonb WHERE id=%s", (json.dumps([note]), report_id))


def to_review(report_id: str, note: str) -> None:
    add_note(report_id, note)
    set_report(report_id, "in_review")


def writer_model(report_id: str) -> str | None:
    with pool().connection() as c:
        r = c.execute("SELECT model FROM ai_calls WHERE report_id=%s AND role='writer' AND outcome IN ('ok','fallback') "
                      "ORDER BY id DESC LIMIT 1", (report_id,)).fetchone()
    return gw.base_model(r["model"]) if r else None


# --------------------------------------------------------------------------------------------- parallel calls
def run_calls(ctx: Ctx, role: str, payloads: list[dict], exclude: tuple[str, ...] = ()) -> list[gw.Completion]:
    """Run the stage's model calls in parallel threads. The first failure ends the stage: the others are
    cancelled (cancel switch; in live mode this closes the stage's SDK client), and if a call thread does
    not stop within CANCEL_WAIT_S its reservation is closed and the child is recycled."""
    if not payloads:
        return []
    calls = ctx.calls()
    ex = cf.ThreadPoolExecutor(min(8, len(payloads)), thread_name_prefix=f"call-{role}")
    # each call runs in a copy of this thread's context, so its gateway spans join the stage's trace
    futs = [ex.submit(contextvars.copy_context().run, gw.complete, role, p, calls=calls, report_id=ctx.report_id,
                      purpose=ctx.purpose, exclude=exclude)
            for p in payloads]
    try:
        for f in cf.as_completed(futs):
            f.result()
        out = [f.result() for f in futs]
    except BaseException as e:
        calls.cancel.set()
        _, alive = cf.wait(futs, timeout=settings.cancel_wait_s)
        if alive:
            ids = gw.abandon(calls)
            raise Recycle(f"{len(alive)} call thread(s) did not stop after cancellation; calls {ids} closed as cancelled") from e
        raise
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        if hasattr(gw.provider(), "release"):
            gw.provider().release(calls.cancel)
    for c in out:
        ctx.models[role] = c.model
        ctx.prompts[role] = c.prompt_version
        if c.fallback:
            ctx.notes.append(f"{role}: fallback model {c.model} used")
    return out


# --------------------------------------------------------------------------------------------- execution
def _timeouts(report_id: str, stage: str) -> int:
    n = rds().incr(f"timeouts:{report_id}:{stage}")
    rds().expire(f"timeouts:{report_id}:{stage}", 86400)
    return n


def load_ctx(report_id: str, owner: str) -> Ctx:
    with pool().connection() as c:
        r = c.execute("SELECT q.payload, q.plan, q.clarification, k.role FROM reports r JOIN requests q ON q.id=r.request_id "
                      "JOIN api_keys k ON k.id=q.api_key_id WHERE r.id=%s", (report_id,)).fetchone()
    purpose = (r["plan"] or {}).get("purpose", "live")
    return Ctx(report_id, r["payload"], r["plan"] or {}, (r["clarification"] or {}).get("answers", {}), purpose, owner,
               time.monotonic())


def recycle_child(report_id: str, stage: str, owner: str, reason: str) -> None:
    """Free the stage and end this child process. The pool starts a fresh child; because the task is
    acks_late with reject_on_worker_lost, Celery requeues the message and the claim lets exactly one
    execution take the stage again. The stuck thread ends with the process."""
    if _timeouts(report_id, stage) >= 2:
        release(report_id, stage, owner, "failed", f"child recycled after a second time-out: {reason}")
        to_review(report_id, f"stage {stage}: call threads hung twice")
    else:
        release(report_id, stage, owner, "pending", f"child recycled: {reason}")
    rds().rpush("recycled", json.dumps({"report_id": report_id, "stage": stage, "pid": os.getpid(),
                                        "threads": threading.active_count(), "at": time.time()}))
    log.warning("recycling child pid %s after stage %s of %s: %s", os.getpid(), stage, report_id, reason)
    logging.shutdown()
    os._exit(70)


def execute_stage(stage: str, report_id: str, fn: Callable[[Ctx], StageResult]) -> str:
    """Run one stage. Returns 'done', 'skip' (duplicate message), 'retry' (try again shortly),
    'provider_wait' (retry in PROVIDER_RETRY_S) or 'stop' (report went to review or failed)."""
    owner = uuid.uuid4().hex
    state = claim(report_id, stage, owner)
    if state == "done":
        return "done"
    if state == "busy":
        return "skip"
    if stage == "collect":
        set_report(report_id, "running")
    fault = rds().getdel(f"fault:r:{report_id}:stage:{stage}") or rds().get(f"fault:stage:{stage}")
    try:
        if fault == "hang_hard":                      # drill: code that swallows the soft limit
            while True:
                try:
                    time.sleep(1)
                except SoftTimeLimitExceeded:
                    pass
        if fault == "crash":                          # drill: an unexpected exception inside the stage
            raise RuntimeError("injected stage crash")
        ctx = load_ctx(report_id, owner)
        result = fn(ctx)
    except Recycle as e:
        recycle_child(report_id, stage, owner, str(e))
        return "stop"                                 # not reached outside tests (os._exit)
    except (SoftTimeLimitExceeded, StageDeadline, CallCancelled) as e:
        why = f"soft time limit {settings.stage_soft_limit_s:g} s" if isinstance(e, SoftTimeLimitExceeded) \
            else f"stage deadline: {e}"
        if _timeouts(report_id, stage) >= 2:
            release(report_id, stage, owner, "failed", f"{why}, second time-out")
            to_review(report_id, f"stage {stage} timed out twice")
            return "stop"
        release(report_id, stage, owner, "pending", why)
        return "retry"
    except ProviderUnavailable as e:
        release(report_id, stage, owner, "pending", f"provider unavailable: {e}")
        set_report(report_id, "waiting_provider")
        return "provider_wait"
    except (InvalidOutput, NoIndependentModel, BudgetExhausted) as e:
        release(report_id, stage, owner, "failed", str(e))
        to_review(report_id, f"stage {stage} stopped: {e}")
        return "stop"
    except Exception as e:                            # unexpected: free the stage at once; the reaper restarts it
        log.exception("stage %s of %s failed", stage, report_id)
        release(report_id, stage, owner, "pending", f"{type(e).__name__}: {e}")
        raise
    if not commit(ctx, stage, result):
        return "skip"                                 # lease lost: another execution owns the stage
    if stage == "collect":
        set_report(report_id, "running")
    with pool().connection() as c:                    # a stage that succeeds after a provider wait resumes
        c.execute("UPDATE reports SET status='running' WHERE id=%s AND status='waiting_provider'", (report_id,))
    return "done"


def reap() -> dict:
    """(1) model calls still 'started' after a lease: their process died; closed as 'lost' with the whole
    reservation booked as spent. (2) Stages whose lease expired (worker killed, or the hard limit killed
    the child). (3) Reports whose next stage is pending with no progress for longer than a lease (a lost
    message). Returns what to restart: [(report_id, stage)]."""
    from ..gateway import budget

    with pool().connection() as c:
        lost = c.execute("SELECT id FROM ai_calls WHERE outcome='started' AND created_at < now() - make_interval(secs => %s)",
                         (settings.stage_lease_s,)).fetchall()
        rows = c.execute("UPDATE report_stages SET status='pending', lease_owner=NULL, lease_until=NULL, "
                         "error='lease expired: worker killed or hard time limit' "
                         "WHERE status='running' AND lease_until < now() RETURNING report_id, stage::text, attempts").fetchall()
        stuck = c.execute(
            "WITH nxt AS (SELECT DISTINCT ON (s.report_id) s.report_id, s.stage, s.status FROM report_stages s "
            "JOIN reports r ON r.id = s.report_id WHERE r.status IN ('queued','running','waiting_provider') "
            "AND s.status <> 'done' ORDER BY s.report_id, s.stage) "
            "SELECT n.report_id, n.stage::text AS stage FROM nxt n JOIN reports r ON r.id = n.report_id "
            "WHERE n.status = 'pending' AND coalesce((SELECT max(finished_at) FROM report_stages d "
            "WHERE d.report_id = n.report_id), r.created_at) < now() - make_interval(secs => %s)",
            (settings.stage_lease_s,)).fetchall()
    for r in lost:
        budget.settle(r["id"], 0, 0, 0, None, "lost")
    restart = [(r["report_id"], r["stage"]) for r in stuck]
    for r in rows:
        if r["attempts"] >= 3:
            with pool().connection() as c:
                c.execute("UPDATE report_stages SET status='failed' WHERE report_id=%s AND stage=%s",
                          (r["report_id"], r["stage"]))
            to_review(r["report_id"], f"stage {r['stage']} lost its worker {r['attempts']} times")
        else:
            restart.append((r["report_id"], r["stage"]))
    return {"lost_calls": len(lost), "expired": len(rows), "stuck": len(stuck), "restart": restart}


def finish(c, report_id: str, status: str, body: dict, json_key: str, html_key: str) -> None:
    """Deliver (or send to review) inside the render stage's fenced commit."""
    cost = c.execute("SELECT coalesce(sum(cost_usd),0) AS s FROM ai_calls WHERE report_id=%s", (report_id,)).fetchone()["s"]
    r = c.execute("SELECT created_at FROM reports WHERE id=%s", (report_id,)).fetchone()
    dur = int((dt.datetime.now(dt.UTC) - r["created_at"]).total_seconds())
    body["cost_usd"], body["duration_s"] = float(cost), dur
    c.execute("UPDATE reports SET status=%s, body=%s, cost_usd=%s, duration_s=%s, json_object_key=%s, html_object_key=%s, "
              "notes=%s WHERE id=%s", (status, json.dumps(body, ensure_ascii=False, default=str), cost, dur, json_key,
                                       html_key, json.dumps(body.get("notes", [])), report_id))
    c.execute("UPDATE requests SET status=%s, updated_at=now() WHERE id=(SELECT request_id FROM reports WHERE id=%s)",
              (status, report_id))
