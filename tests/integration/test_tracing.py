"""OpenTelemetry tracing: spans are captured in memory (no collector needed) and checked for the structure
and attributes the Jaeger view relies on: gateway spans with GenAI attributes, error status on a failed
attempt, the API trace id returned to the client, and the trace context carried in Celery task headers."""
import json
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from masader import telemetry
from masader.gateway import client as gw
from masader.redisclient import rds

pytestmark = pytest.mark.db
PAYLOAD = {"assertions": [{"n": 1, "text": "Tankers fell 18 percent.", "cites": [1], "evidence": ["Tankers fell 18 percent."]}]}
_EXPORTER = InMemorySpanExporter()


@pytest.fixture
def spans(db):
    from masader.store.db import reset_pool

    assert telemetry.setup("masader-test", span_processor=SimpleSpanProcessor(_EXPORTER))
    reset_pool()                          # new connections are created through the psycopg instrumentation
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def by_name(exporter, prefix):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def test_gateway_call_is_traced_with_genai_attributes(spans):
    with telemetry.tracer.start_as_current_span("stage") as root:
        res = gw.complete("support", PAYLOAD, calls=gw.StageCalls(deadline=time.monotonic() + 200), report_id="rep_t")
    (complete,) = by_name(spans, "llm.complete support")
    (call,) = by_name(spans, "llm.call support")
    assert call.parent.span_id == complete.context.span_id
    assert complete.parent.span_id == root.get_span_context().span_id
    a = call.attributes
    assert a["gen_ai.provider.name"] == "simulated" and a["gen_ai.request.model"] == "claude-haiku-5-5"
    assert a["gen_ai.usage.input_tokens"] == res.input_tokens > 0 and a["gen_ai.usage.output_tokens"] == res.output_tokens
    assert a["masader.report_id"] == "rep_t" and a["masader.outcome"] == "returned"
    assert complete.attributes["masader.fallback"] is False and complete.status.status_code != StatusCode.ERROR
    # budget reservation and settlement queries appear in the same trace
    db_spans = [s for s in spans.get_finished_spans() if s.attributes.get("db.system.name", s.attributes.get("db.system"))]
    assert db_spans and all(s.context.trace_id == root.get_span_context().trace_id for s in db_spans)
    assert all("Tankers" not in json.dumps(dict(s.attributes)) for s in spans.get_finished_spans())   # no source text on spans


def test_failed_attempt_is_an_error_span_and_fallback_is_recorded(spans):
    rds().rpush("fault:r:rep_t:support", "error", "error")       # primary fails twice, the fallback answers
    res = gw.complete("support", PAYLOAD, calls=gw.StageCalls(deadline=time.monotonic() + 200), report_id="rep_t")
    calls = by_name(spans, "llm.call support")
    assert [c.attributes["gen_ai.request.model"] for c in calls] == ["claude-haiku-5-5", "claude-haiku-5-5", "claude-haiku-4-5"]
    assert [c.status.status_code for c in calls[:2]] == [StatusCode.ERROR, StatusCode.ERROR]
    assert calls[2].attributes["masader.outcome"] == "returned"
    (complete,) = by_name(spans, "llm.complete support")
    assert res.fallback and complete.attributes["masader.fallback"] is True
    assert complete.attributes["masader.outcome"] == "fallback"


def test_api_request_span_and_trace_id_header(spans, client, keys):
    from masader.api.app import app

    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        app.middleware_stack = None       # earlier tests already built the stack; rebuild it with tracing
        telemetry.instrument_fastapi(app)
    r = client.get("/v1/me", headers={"X-API-Key": keys["requester"]})
    assert r.status_code == 200
    server = [s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
    assert server and server[-1].attributes.get("http.route") == "/v1/me"
    assert r.headers["X-Trace-Id"] == format(server[-1].context.trace_id, "032x")
    assert not [s for s in server if s.attributes.get("http.route") == "/metrics"]
    client.get("/metrics")
    assert not [s for s in spans.get_finished_spans() if s.attributes.get("http.route") == "/metrics"]


def test_trace_context_travels_in_celery_task_headers(spans):
    from masader.pipeline.tasks import TASKS

    rds().delete("cpu")
    with telemetry.tracer.start_as_current_span("request") as root:
        TASKS["collect"].apply_async(("rep_trace",))
    raw = rds().lpop("cpu")
    rds().delete("cpu")
    headers = json.loads(raw)["headers"]
    trace_id = format(root.get_span_context().trace_id, "032x")
    assert headers["traceparent"].split("-")[1] == trace_id
    (publish,) = [s for s in spans.get_finished_spans() if s.name.startswith("apply_async/masader.stage.collect")]
    assert publish.kind == trace.SpanKind.PRODUCER and publish.context.trace_id == root.get_span_context().trace_id


def test_tracing_is_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.enabled() is False
