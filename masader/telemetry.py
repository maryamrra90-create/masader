"""Distributed tracing with OpenTelemetry: one trace per report, from the HTTP request through every Celery
stage to each model call in the gateway.

Off by default. It is switched on per process by setting OTEL_EXPORTER_OTLP_ENDPOINT (for example
http://jaeger:4318 in `docker compose --profile monitoring`); spans are then exported over OTLP/HTTP.
When it is off, `tracer` is OpenTelemetry's no-op proxy, so the spans in the code cost almost nothing.

What is traced:
  * API: every request (FastAPI instrumentation; /metrics and /healthz excluded). Error bodies carry the
    trace id, so a failed call can be looked up in Jaeger.
  * Pipeline: Celery publish and run spans; the trace context travels in the task message headers, so the
    eight stages of a report (a Celery chain) join the trace of the request that started it.
  * Database: psycopg queries.
  * Gateway: `llm.complete` per role call and `llm.call` per provider attempt, with GenAI semantic-convention
    attributes (gen_ai.request.model, gen_ai.usage.input_tokens / output_tokens) plus the role, outcome,
    fallback and estimated cost. Prompts and source text are never put on spans.
"""
from __future__ import annotations

import os
import threading

from opentelemetry import trace

tracer = trace.get_tracer("masader")
_lock = threading.Lock()
_configured = False


def enabled() -> bool:
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def setup(service: str, *, span_processor=None) -> bool:
    """Install the tracer provider and library instrumentation for this process, once.

    `span_processor` lets tests capture spans in memory; otherwise an OTLP/HTTP exporter is used and the
    call does nothing unless OTEL_EXPORTER_OTLP_ENDPOINT is set. Returns True when tracing is active."""
    global _configured
    if span_processor is None and not enabled():
        return False
    with _lock:
        if _configured:
            return True
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        from .settings import settings

        try:
            from importlib.metadata import version as _version
            service_version = _version("masader")
        except Exception:  # noqa: BLE001 - running from a source checkout that is not installed
            service_version = "1.0.0"

        provider = TracerProvider(resource=Resource.create({
            "service.name": os.environ.get("OTEL_SERVICE_NAME", service),
            "service.version": service_version,
            "deployment.environment.name": settings.env,
            "masader.model_mode": settings.model_mode,
        }))
        if span_processor is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            span_processor = BatchSpanProcessor(OTLPSpanExporter())   # endpoint from OTEL_EXPORTER_OTLP_ENDPOINT
        provider.add_span_processor(span_processor)
        trace.set_tracer_provider(provider)
        CeleryInstrumentor().instrument()
        PsycopgInstrumentor().instrument()
        _configured = True
        return True


def instrument_fastapi(app) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,healthz")


def current_trace_id() -> str | None:
    """The active trace id as 32 hex digits, or None when no span is recording."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def shutdown() -> None:
    """Flush buffered spans (called before a worker child exits)."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
