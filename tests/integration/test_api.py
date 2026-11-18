"""The /v1 API: authentication, idempotency, input policy, rate and admission limits, clarification,
reports and exports, publication, corrections, review, keys and operations endpoints."""
import uuid

import pytest

from masader.redisclient import rds
from tests.conftest import HORMUZ, idem, request_report, run_pipeline

pytestmark = pytest.mark.db


def H(key, **extra):
    return {"X-API-Key": key, **extra}


def test_auth(client, keys):
    assert client.post("/v1/requests", json=HORMUZ, headers=idem()).status_code == 401
    r = client.post("/v1/requests", json=HORMUZ, headers=H("rq_" + "x" * 40, **idem()))
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized" and r.headers["X-Trace-Id"]
    assert client.get("/v1/me", headers=H(keys["reviewer"])).json()["role"] == "reviewer"


def test_validation_errors(client, keys):
    bad = [dict(HORMUZ, extra=1), dict(HORMUZ, questions=[]), dict(HORMUZ, geography=["om"]),
           dict(HORMUZ, **{"from": "2026-10-20", "to": "2026-10-01"}), dict(HORMUZ, **{"from": "2024-01-01"})]
    for b in bad:
        r = client.post("/v1/requests", json=b, headers=H(keys["requester"], **idem()))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request", b
    assert client.post("/v1/requests", json=HORMUZ, headers=H(keys["requester"])).status_code == 400   # no Idempotency-Key


def test_idempotency(client, keys):
    h = H(keys["requester"], **idem())
    a = client.post("/v1/requests", json=HORMUZ, headers=h)
    b = client.post("/v1/requests", json=HORMUZ, headers=h)
    assert a.status_code == b.status_code == 200 and a.json()["request_id"] == b.json()["request_id"]
    c = client.post("/v1/requests", json=dict(HORMUZ, topic="Something else entirely"), headers=h)
    assert c.status_code == 409 and c.json()["error"]["code"] == "idempotency_conflict"


def test_input_policy_and_redaction(client, keys, db):
    r = client.post("/v1/requests", json=dict(HORMUZ, topic="CONFIDENTIAL memo on Hormuz shipping"),
                    headers=H(keys["requester"], **idem()))
    assert r.status_code == 422 and r.json()["error"]["code"] == "rejected_by_input_policy"
    body = dict(HORMUZ, questions=["What happened to shipping? Reply to ali@example.com please"])
    r = client.post("/v1/requests", json=body, headers=H(keys["requester"], **idem()))
    with db.connection() as c:
        row = c.execute("SELECT payload, redactions FROM requests WHERE id=%s", (r.json()["request_id"],)).fetchone()
    assert "ali@example.com" not in str(row["payload"]) and row["redactions"] == ["EMAIL"]


def test_clarification_flow_and_answer_validation(client, keys, started):
    r = client.post("/v1/requests", json=HORMUZ, headers=H(keys["requester"], **idem()))
    assert r.status_code == 200 and r.json()["questions"][0]["default"] == "include"
    url = r.json()["answers_url"]
    assert client.post(url, json={"zz": "x"}, headers=H(keys["requester"])).status_code == 400
    assert client.post(url, json={"q1": "maybe"}, headers=H(keys["requester"])).status_code == 400
    assert client.post(url, json={"q1": "include"}, headers=H(keys["other"])).status_code == 404
    a = client.post(url, json={"q1": "include"}, headers=H(keys["requester"]))
    b = client.post(url, json={"q1": "include"}, headers=H(keys["requester"]))
    assert a.status_code == b.status_code == 202 and a.json()["report_id"] == b.json()["report_id"] and len(started) == 1
    g = client.get(f"/v1/requests/{a.json()['request_id']}", headers=H(keys["requester"])).json()
    assert g["clarification"]["answers"] == {"q1": "include"}


@pytest.mark.parametrize("topic,qid", [("Apple supply chain in the Gulf", "entity_apple"),
                                       ("Predict the oil price in the Gulf next month", "scope")])
def test_planner_rules(client, keys, topic, qid):
    body = dict(HORMUZ, topic=topic, questions=["What is happening in the region?"])
    r = client.post("/v1/requests", json=body, headers=H(keys["requester"], **idem()))
    assert qid in [q["id"] for q in r.json()["questions"]]


def test_too_little_evidence_offers_to_widen(client, keys, db):
    body = dict(HORMUZ, topic="Lithium mining in the Andes", questions=["Which mines opened this week?"])
    r = client.post("/v1/requests", json=body, headers=H(keys["requester"], **idem()))
    q = {x["id"]: x for x in r.json()["questions"]}
    assert "widen" in q
    a = client.post(r.json()["answers_url"], json={"widen": "widen_30_days"}, headers=H(keys["requester"]))
    with db.connection() as c:
        p = c.execute("SELECT payload FROM requests WHERE id=%s", (a.json()["request_id"],)).fetchone()["payload"]
    assert p["from"] == "2026-09-19"


def test_planner_model_failure_falls_back_to_rules(client, keys, db):
    rds().set("fault:claude-haiku-5-5", "error")
    r = client.post("/v1/requests", json=HORMUZ, headers=H(keys["requester"], **idem()))
    assert r.status_code == 202                                      # no model question, so straight to the queue
    with db.connection() as c:
        plan = c.execute("SELECT plan FROM requests WHERE id=%s", (r.json()["request_id"],)).fetchone()["plan"]
    assert plan["planner"] == "rules" and "rule-based plan" in plan["planner_note"]


def test_rate_limit(client, db):
    from masader.cli import create_key
    _, k = create_key("Slow", "slow@example.org", "requester", rate=2)
    body = dict(HORMUZ, entities=["shipping"], sources={"prefer": ["official"]})
    codes = [client.post("/v1/requests", json=body, headers=H(k, **idem())).status_code for _ in range(3)]
    assert codes[:2] == [202, 202] and codes[2] == 429


def test_admission_limit(client, keys, started):
    body = dict(HORMUZ, sources={"prefer": ["official"]})
    codes = [client.post("/v1/requests", json=dict(body, topic=f"Shipping in the Strait of Hormuz {i}"),
                         headers=H(keys["admin"], **idem())) for i in range(7)]
    assert [c.status_code for c in codes[:6]] == [202] * 6
    assert codes[6].status_code == 429 and codes[6].json()["error"]["code"] == "busy" and codes[6].headers["Retry-After"] == "60"
    assert len(started) == 6


def test_budget_admission(client, keys, db):
    with db.connection() as c:
        c.execute("UPDATE budgets SET spent_usd = cap_usd WHERE purpose='live'")
    r = client.post("/v1/requests", json=dict(HORMUZ, sources={"prefer": ["official"]}), headers=H(keys["requester"], **idem()))
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exhausted"


def test_eval_purpose_needs_admin(client, keys):
    r = client.post("/v1/requests", json=dict(HORMUZ, purpose="eval"), headers=H(keys["requester"], **idem()))
    assert r.status_code == 403


def test_report_access_etag_exports_and_cache(client, keys, started):
    rid = request_report(client, keys["requester"])
    assert client.get(f"/v1/reports/{rid}", headers=H(keys["other"])).status_code == 404
    assert client.get(f"/v1/reports/{rid}.json", headers=H(keys["requester"])).status_code == 409
    run_pipeline(rid)
    r = client.get(f"/v1/reports/{rid}", headers=H(keys["requester"]))
    assert r.status_code == 200 and r.json()["status"] == "delivered" and len(r.json()["stages"]) == 8
    assert r.json()["findings"][0]["sources"][0]["passage"]
    assert client.get(f"/v1/reports/{rid}", headers=H(keys["requester"], **{"If-None-Match": r.headers["ETag"]})).status_code == 304
    j = client.get(f"/v1/reports/{rid}.json", headers=H(keys["requester"]))
    assert j.json()["report_id"] == rid and "attachment" in j.headers["Content-Disposition"]
    h = client.get(f"/v1/reports/{rid}.html", headers=H(keys["reviewer"]))
    assert h.status_code == 200 and "Findings and their evidence" in h.text and "default-src 'none'" in h.headers["Content-Security-Policy"]
    again = client.post("/v1/requests", json=HORMUZ, headers=H(keys["requester"], **idem()))
    acc = client.post(again.json()["answers_url"], json={}, headers=H(keys["requester"]))
    assert acc.json()["report_id"] != rid                            # answers path always runs a fresh report
    body = dict(HORMUZ, sources={"prefer": ["official"]})
    first = request_report(client, keys["requester"], body)
    run_pipeline(first)
    cached = client.post("/v1/requests", json=body, headers=H(keys["requester"], **idem())).json()
    assert cached["cached"] is True and cached["report_id"] == first


def test_publish_withdraw_and_public_page(client, keys):
    rid = request_report(client, keys["requester"])
    assert client.post(f"/v1/reports/{rid}/publish", headers=H(keys["reviewer"])).status_code == 409   # not delivered yet
    run_pipeline(rid)
    assert client.post(f"/v1/reports/{rid}/publish", headers=H(keys["requester"])).status_code == 403
    assert client.get(f"/r/{rid}").status_code == 404
    for _ in range(2):                                                # publishing twice returns the same state
        r = client.post(f"/v1/reports/{rid}/publish", headers=H(keys["reviewer"]))
        assert r.status_code == 200 and r.json()["publication"] == "published"
    page = client.get(f"/r/{rid}")
    assert page.status_code == 200 and "a reviewer approved it" in page.text
    assert client.get(f"/v1/reports/{rid}", headers=H(keys["other"])).status_code == 200    # published: visible to all keys
    assert client.post(f"/v1/reports/{rid}/withdraw", json={"approve": False, "note": "source retracted"},
                       headers=H(keys["reviewer"])).json()["publication"] == "withdrawn"
    assert client.get(f"/r/{rid}").status_code == 410


def test_published_report_is_immutable(client, keys, db):
    import psycopg
    rid = request_report(client, keys["requester"])
    run_pipeline(rid)
    client.post(f"/v1/reports/{rid}/publish", headers=H(keys["reviewer"]))
    with pytest.raises(psycopg.errors.RaiseException):
        with db.connection() as c:
            c.execute("UPDATE reports SET body='{}'::jsonb WHERE id=%s", (rid,))


def test_correction_creates_a_new_version(client, keys, db):
    rid = request_report(client, keys["requester"])
    run_pipeline(rid)
    client.post(f"/v1/reports/{rid}/publish", headers=H(keys["reviewer"]))
    f = client.get(f"/v1/reports/{rid}", headers=H(keys["requester"])).json()["findings"][0]
    assert client.post(f"/v1/reports/{rid}/corrections", json={"claim_id": 999999, "reason": "wrong claim"},
                       headers=H(keys["other"])).status_code == 400
    c1 = client.post(f"/v1/reports/{rid}/corrections", json={"claim_id": f["claim_id"], "reason": "The figure was revised"},
                     headers=H(keys["other"]))
    c2 = client.post(f"/v1/reports/{rid}/corrections", json={"claim_id": f["claim_id"], "reason": "The figure was revised"},
                     headers=H(keys["other"]))
    assert c1.status_code == 201 and c2.json()["duplicate"] is True
    q = client.get("/v1/review/queue", headers=H(keys["reviewer"])).json()
    assert q["corrections"][0]["id"] == c1.json()["correction_id"]
    assert client.get("/v1/review/queue", headers=H(keys["requester"])).status_code == 403
    res = client.post(f"/v1/corrections/{c1.json()['correction_id']}/resolve",
                      json={"accept": True, "note": "Corrected after check", "corrected_text": "Corrected sentence."},
                      headers=H(keys["reviewer"])).json()
    new = client.get(f"/v1/reports/{res['new_report_id']}", headers=H(keys["requester"])).json()
    old = client.get(f"/v1/reports/{rid}", headers=H(keys["requester"])).json()
    assert new["version"] == 2 and new["supersedes"] == rid and new["publication"] == "published"
    assert old["publication"] == "superseded" and old["superseded_by"] == new["report_id"]
    assert new["findings"][0]["text"] == "Corrected sentence." and old["findings"][0]["text"] == f["text"]
    with db.connection() as c:
        assert c.execute("SELECT review_status FROM claim_labels WHERE claim_id=%s",
                         (f["claim_id"],)).fetchone()["review_status"] == "human_reviewed"


def test_review_decision_for_in_review_report(client, keys, db):
    body = dict(HORMUZ, questions=["Was there a hostage incident on a tanker this week?"])
    rid = request_report(client, keys["requester"], body)
    run_pipeline(rid)
    assert client.post(f"/v1/review/{rid}", json={"approve": True, "note": "checked"}, headers=H(keys["requester"])).status_code == 403
    r = client.post(f"/v1/review/{rid}", json={"approve": True, "note": "checked by a person"}, headers=H(keys["reviewer"]))
    assert r.json()["status"] == "delivered"
    rep = client.get(f"/v1/reports/{rid}", headers=H(keys["requester"])).json()
    assert rep["review_status"] == "reviewed"
    assert client.post(f"/v1/review/{rid}", json={"approve": True, "note": "again"}, headers=H(keys["reviewer"])).status_code == 409


def test_key_management(client, keys):
    r = client.post("/v1/keys", json={"holder_name": "New", "holder_email": "new@example.org"}, headers=H(keys["admin"]))
    assert r.status_code == 201
    new = r.json()["api_key"]
    assert client.get("/v1/me", headers=H(new)).status_code == 200
    assert client.post("/v1/keys", json={"holder_name": "XY", "holder_email": "x@example.org"}, headers=H(keys["reviewer"])).status_code == 403
    assert client.delete(f"/v1/keys/{r.json()['key_id']}", headers=H(keys["admin"])).status_code == 200
    assert client.get("/v1/me", headers=H(new)).status_code == 401
    assert client.delete(f"/v1/keys/{uuid.uuid4()}", headers=H(keys["admin"])).status_code == 404


def test_ops_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"] == {"postgres": "ok", "redis": "ok"}
    m = client.get("/metrics").text
    assert "masader_http_request_seconds" in m and "masader_budget_remaining_usd" in m
    assert "<form" in client.get("/").text


def test_queue_down_returns_503_and_stores_nothing(client, keys, db, monkeypatch):
    import redis as redislib

    from masader.api import app as appmod

    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise redislib.exceptions.ConnectionError("down")
            return fail
    monkeypatch.setattr(appmod, "rds", lambda: Down())
    r = client.post("/v1/requests", json=dict(HORMUZ, sources={"prefer": ["official"]}), headers=H(keys["requester"], **idem()))
    assert r.status_code == 503 and r.json()["error"]["code"] == "queue_unavailable"
    with db.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM reports").fetchone()["n"] == 0


def test_delete_user_data(client, keys, db):
    from masader.cli import delete_user
    rid = request_report(client, keys["requester"])
    run_pipeline(rid)
    pub = request_report(client, keys["requester"], dict(HORMUZ, sources={"prefer": ["official"]}))
    run_pipeline(pub)
    client.post(f"/v1/reports/{pub}/publish", headers=H(keys["reviewer"]))
    out = delete_user("requester@example.org")
    assert out == {"keys": 1, "reports_deleted": 1, "published_kept": 1}
    with db.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM reports WHERE id=%s", (rid,)).fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM api_keys WHERE holder_email='requester@example.org'").fetchone()["n"] == 0
    assert client.get(f"/r/{pub}").status_code == 200
    assert client.get("/v1/me", headers=H(keys["requester"])).status_code == 401
