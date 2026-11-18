"""The gateway against real PostgreSQL (budgets, ai_calls) and Redis (circuit, fault injection), with the
simulated provider. Faults are injected the same way the drills do."""
import threading
import time

import pytest

from masader.gateway import budget, circuit
from masader.gateway import client as gw
from masader.gateway.errors import (BudgetExhausted, CallCancelled, InvalidOutput, NoIndependentModel, ProviderUnavailable,
                                    StageDeadline)
from masader.redisclient import rds

pytestmark = pytest.mark.db
PAYLOAD = {"assertions": [{"n": 1, "text": "Tankers fell 18 percent.", "cites": [1], "evidence": ["Tankers fell 18 percent."]}]}


def calls(seconds=200):
    return gw.StageCalls(deadline=time.monotonic() + seconds)


def outcomes(db):
    with db.connection() as c:
        return [(r["model"], r["outcome"]) for r in c.execute("SELECT model, outcome FROM ai_calls ORDER BY id")]


def test_ok_call_reserves_and_settles(db):
    before = budget.remaining("live")
    res = gw.complete("support", PAYLOAD, calls=calls())
    assert res.output["results"][0]["result"] == "pass" and res.model == "sim:claude-haiku-5-5" and not res.fallback
    assert outcomes(db) == [("sim:claude-haiku-5-5", "ok")]
    with db.connection() as c:
        b = c.execute("SELECT reserved_usd, spent_usd FROM budgets WHERE purpose='live'").fetchone()
    assert float(b["reserved_usd"]) == 0 and 0 < float(b["spent_usd"]) <= before


def test_fast_error_is_retried_once_on_the_same_model(db):
    rds().rpush("fault:r:rep_t:support", "error")
    res = gw.complete("support", PAYLOAD, calls=calls(), report_id="rep_t")
    assert not res.fallback
    assert outcomes(db) == [("sim:claude-haiku-5-5", "error"), ("sim:claude-haiku-5-5", "ok")]


def test_timeout_goes_straight_to_the_fallback(db):
    rds().rpush("fault:r:rep_t:support", "timeout")
    res = gw.complete("support", PAYLOAD, calls=calls(), report_id="rep_t", timeout_s=0.2)
    assert res.fallback and res.model == "sim:claude-haiku-4-5"
    assert outcomes(db) == [("sim:claude-haiku-5-5", "timeout"), ("sim:claude-haiku-4-5", "fallback")]
    with db.connection() as c:                 # a timed-out call keeps its whole reservation as spent
        r = c.execute("SELECT reserved_usd FROM ai_calls WHERE outcome='timeout'").fetchone()
        assert float(r["reserved_usd"]) > 0


def test_invalid_json_gets_one_repair_attempt(db):
    rds().set("fault:claude-haiku-5-5", "invalid_json_once")
    res = gw.complete("support", PAYLOAD, calls=calls())
    assert not res.fallback
    assert [o for _, o in outcomes(db)] == ["invalid_json", "ok"]


def test_invalid_json_twice_is_invalid_output(db):
    rds().rpush("fault:r:rep_t:support", "invalid_json", "invalid_json")
    with pytest.raises(InvalidOutput):
        gw.complete("support", PAYLOAD, calls=calls(), report_id="rep_t")


def test_refusal_is_not_retried_on_the_same_model(db):
    rds().rpush("fault:r:rep_t:support", "refusal")
    res = gw.complete("support", PAYLOAD, calls=calls(), report_id="rep_t")
    assert res.fallback and [o for _, o in outcomes(db)] == ["refusal", "fallback"]


def test_both_models_down_is_provider_unavailable_and_opens_the_circuit(db):
    rds().set("fault:claude-haiku-5-5", "error")
    rds().set("fault:claude-haiku-4-5", "error")
    for _ in range(3):
        with pytest.raises(ProviderUnavailable):
            gw.complete("support", PAYLOAD, calls=calls())
    assert circuit.is_open("claude-haiku-5-5") and circuit.is_open("claude-haiku-4-5")
    n = len(outcomes(db))
    with pytest.raises(ProviderUnavailable):     # open circuit: no call is made at all
        gw.complete("support", PAYLOAD, calls=calls())
    assert len(outcomes(db)) == n


def test_validator_independence(db):
    res = gw.complete("support", PAYLOAD, calls=calls(), exclude=("claude-sonnet-5-5",))
    assert res.model == "sim:claude-haiku-5-5"
    rds().set("fault:claude-haiku-5-5", "error")
    with pytest.raises(NoIndependentModel):      # the writer used the fallback model, the primary is down
        gw.complete("support", PAYLOAD, calls=calls(), exclude=("claude-haiku-4-5",))


def test_budget_cap_refuses_the_call_before_it_is_made(db):
    with db.connection() as c:
        c.execute("UPDATE budgets SET spent_usd = cap_usd - 0.00001 WHERE purpose='live'")
    with pytest.raises(BudgetExhausted):
        gw.complete("writer", {"claims": []}, calls=calls())
    assert outcomes(db) == []


def test_concurrent_reservations_never_overspend(db):
    """Many threads reserve against a cap that fits only some of them; the CHECK constraint and the
    conditional UPDATE keep reserved + spent <= cap."""
    amount = budget.worst_case("claude-sonnet-5-5", 2000, 8192)
    with db.connection() as c:
        c.execute("UPDATE budgets SET spent_usd = cap_usd - %s WHERE purpose='live'", (amount * 5.5,))
    ok, refused = [], []

    def one():
        try:
            ok.append(budget.reserve(None, "live", "writer", "x", "p", amount))
        except BudgetExhausted:
            refused.append(1)
    ts = [threading.Thread(target=one) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 5 and len(refused) == 15
    with db.connection() as c:
        b = c.execute("SELECT cap_usd, reserved_usd, spent_usd FROM budgets WHERE purpose='live'").fetchone()
    assert b["reserved_usd"] + b["spent_usd"] <= b["cap_usd"]


def test_stage_deadline_and_cancel(db):
    with pytest.raises(StageDeadline):
        gw.complete("support", PAYLOAD, calls=calls(seconds=5))           # less than LLM_MIN_CALL_S left
    c = calls()
    rds().set("fault:claude-haiku-5-5", "hang")
    t = threading.Timer(0.3, c.cancel.set)
    t.start()
    with pytest.raises(CallCancelled):
        gw.complete("support", PAYLOAD, calls=c)
    assert outcomes(db)[-1][1] == "cancelled"


def test_abandon_books_stuck_calls(db):
    c = calls()
    cid = budget.reserve("rep_t", "live", "support", "sim:x", "p", 0.01)
    c.inflight[cid] = 0.01
    assert gw.abandon(c) == [cid]
    with db.connection() as cn:
        r = cn.execute("SELECT outcome FROM ai_calls WHERE id=%s", (cid,)).fetchone()
        b = cn.execute("SELECT reserved_usd, spent_usd FROM budgets WHERE purpose='live'").fetchone()
    assert r["outcome"] == "cancelled" and float(b["reserved_usd"]) == 0 and float(b["spent_usd"]) == 0.01
