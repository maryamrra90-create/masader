"""The eight stages on the synthetic corpus, the claim / fenced-commit protocol and the reaper."""
import json

import pytest

from masader.pipeline import kit, tasks
from masader.pipeline.kit import STAGES
from masader.redisclient import rds
from tests.conftest import HORMUZ, request_report, run_pipeline

pytestmark = pytest.mark.db


def report(db, rid):
    with db.connection() as c:
        return c.execute("SELECT * FROM reports WHERE id=%s", (rid,)).fetchone()


def test_full_pipeline_delivers_a_labelled_brief(client, keys, db):
    rid = request_report(client, keys["requester"])
    assert run_pipeline(rid) == ["done"] * 8
    r = report(db, rid)
    assert r["status"] == "delivered" and r["body"]["findings"]
    labels = r["stats"]["labels"]
    assert labels["corroborated"] >= 3 and labels["disputed"] >= 2 and labels["insufficient"] >= 1
    body = r["body"]
    assert body["method"]["simulated"] is True and body["sections"]["disputed"]
    text = json.dumps(body["sections"])
    assert "doubled" not in text                                      # the injection item left no trace
    gap = " ".join(body["sections"]["evidence_gaps"])
    assert "every 6 hours" in gap and "superseded" in gap             # the revised figure replaced the old one
    corr = [f for f in body["findings"] if f["label"] == "corroborated"]
    assert any("official statement" in f["reliability"]["reason"] for f in corr)
    for f in body["findings"]:                                        # every finding links to real passages
        assert f["sources"] and all(s["passage"] for s in f["sources"])
    with db.connection() as c:
        n = c.execute("SELECT count(*) AS n FROM ai_calls WHERE report_id=%s AND outcome='started'", (rid,)).fetchone()["n"]
    assert n == 0
    from masader.store.objectstore import get_store
    assert b"Simulated model output" in get_store().get(r["html_object_key"])


def test_stage_claim_is_exclusive_and_ordered(client, keys, db):
    rid = request_report(client, keys["requester"])
    assert kit.claim(rid, "dedup", "x") == "busy"                     # previous stage not done
    assert kit.claim(rid, "collect", "a") == "claimed"
    assert kit.claim(rid, "collect", "b") == "busy"                   # duplicate message
    assert tasks.FUNCS and kit.execute_stage("collect", rid, tasks.FUNCS["collect"]) == "skip"


def test_fenced_commit_refuses_a_lost_lease(client, keys, db):
    rid = request_report(client, keys["requester"])
    assert kit.claim(rid, "collect", "old") == "claimed"
    with db.connection() as c:                                       # the reaper gave the stage to another execution
        c.execute("UPDATE report_stages SET lease_owner='new' WHERE report_id=%s AND stage='collect'", (rid,))
    ctx = kit.load_ctx(rid, "old")
    assert kit.commit(ctx, "collect", kit.StageResult({"x": 1})) is False


def test_redelivered_stage_is_not_run_twice(client, keys, db):
    rid = request_report(client, keys["requester"])
    run_pipeline(rid)
    assert run_pipeline(rid) == ["done"] * 8                          # every stage already done: chain just continues
    with db.connection() as c:
        att = [r["attempts"] for r in c.execute("SELECT attempts FROM report_stages WHERE report_id=%s", (rid,))]
    assert att == [1] * 8


def test_provider_outage_waits_then_resumes(client, keys, db):
    rid = request_report(client, keys["requester"])
    for s in STAGES[:2]:
        kit.execute_stage(s, rid, tasks.FUNCS[s])
    rds().set("fault:claude-sonnet-5-5", "error")
    rds().set("fault:claude-haiku-4-5", "error")
    assert kit.execute_stage("extract", rid, tasks.FUNCS["extract"]) == "provider_wait"
    assert report(db, rid)["status"] == "waiting_provider"
    rds().delete("fault:claude-sonnet-5-5", "fault:claude-haiku-4-5")
    for m in ("claude-sonnet-5-5", "claude-haiku-4-5"):
        rds().delete(f"cb:open:{m}", f"cb:fail:{m}")
    assert [kit.execute_stage(s, rid, tasks.FUNCS[s]) for s in STAGES[2:]] == ["done"] * 6
    assert report(db, rid)["status"] == "delivered"


def test_invalid_output_twice_sends_report_to_review(client, keys, db):
    rid = request_report(client, keys["requester"])
    for s in STAGES[:5]:
        kit.execute_stage(s, rid, tasks.FUNCS[s])
    rds().rpush(f"fault:r:{rid}:writer", "invalid_json", "invalid_json")
    assert kit.execute_stage("write", rid, tasks.FUNCS["write"]) == "stop"
    r = report(db, rid)
    assert r["status"] == "in_review" and any("invalid output twice" in n for n in r["notes"])


def test_validator_independence_sends_report_to_review(client, keys, db):
    rid = request_report(client, keys["requester"])
    for s in STAGES[:5]:
        kit.execute_stage(s, rid, tasks.FUNCS[s])
    rds().rpush(f"fault:r:{rid}:writer", "error", "error")            # writer falls back to claude-haiku-4-5
    assert kit.execute_stage("write", rid, tasks.FUNCS["write"]) == "done"
    rds().set("fault:claude-haiku-5-5", "error")                      # the support check's primary is down
    assert kit.execute_stage("validate", rid, tasks.FUNCS["validate"]) == "stop"
    assert "no model other than claude-haiku-4-5" in " ".join(report(db, rid)["notes"])


def test_stage_deadline_retries_then_reviews(client, keys, db):
    rid = request_report(client, keys["requester"])
    for s in STAGES[:2]:
        kit.execute_stage(s, rid, tasks.FUNCS[s])

    def slow(ctx):
        from masader.gateway.errors import StageDeadline
        raise StageDeadline("test")
    assert kit.execute_stage("extract", rid, slow) == "retry"
    assert kit.execute_stage("extract", rid, slow) == "stop"
    assert report(db, rid)["status"] == "in_review"


def test_unexpected_error_frees_the_stage(client, keys, db):
    rid = request_report(client, keys["requester"])
    rds().set(f"fault:r:{rid}:stage:collect", "crash")
    with pytest.raises(RuntimeError):
        kit.execute_stage("collect", rid, tasks.FUNCS["collect"])
    with db.connection() as c:
        st = c.execute("SELECT status, error FROM report_stages WHERE report_id=%s AND stage='collect'", (rid,)).fetchone()
    assert st["status"] == "pending" and "injected stage crash" in st["error"]


def test_reaper_recovers_expired_leases_and_lost_calls(client, keys, db):
    rid = request_report(client, keys["requester"])
    assert kit.claim(rid, "collect", "dead-worker") == "claimed"
    from masader.gateway import budget
    cid = budget.reserve(rid, "live", "extractor", "sim:x", "p", 0.02)
    with db.connection() as c:
        c.execute("UPDATE report_stages SET lease_until = now() - interval '1 second' WHERE report_id=%s", (rid,))
        c.execute("UPDATE ai_calls SET created_at = now() - interval '1 hour' WHERE id=%s", (cid,))
    r = kit.reap()
    assert r["lost_calls"] == 1 and (rid, "collect") in r["restart"]
    with db.connection() as c:
        assert c.execute("SELECT outcome FROM ai_calls WHERE id=%s", (cid,)).fetchone()["outcome"] == "lost"
    assert run_pipeline(rid)[-1] == "done"


def test_reaper_restarts_a_report_whose_message_was_lost(client, keys, db):
    rid = request_report(client, keys["requester"])
    with db.connection() as c:
        c.execute("UPDATE reports SET created_at = now() - interval '1 hour' WHERE id=%s", (rid,))
    assert (rid, "collect") in kit.reap()["restart"]


def test_sensitive_topic_goes_to_review(client, keys, db):
    body = dict(HORMUZ, questions=["Was there a terror attack on shipping in the strait?"])
    rid = request_report(client, keys["requester"], body)
    run_pipeline(rid)
    assert report(db, rid)["status"] == "in_review"


def test_official_only_answer_drops_company_statements(client, keys, db):
    rid = request_report(client, keys["requester"], answers={"q1": "official_and_news_only"})
    run_pipeline(rid)
    r = report(db, rid)
    assert r["stats"]["company_statements_dropped"] >= 1
    assert "container line" not in json.dumps(r["body"]["sections"])
