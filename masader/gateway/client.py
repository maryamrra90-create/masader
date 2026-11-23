"""The LLM gateway: the one function every stage uses to call a model (architecture B.3.6, D.1, E.4).

complete(role, payload, ...) does, in order:
  1. route: the role's primary and fallback model from config/routing.yaml, minus `exclude` (the support
     check excludes the writer's model) and minus models whose circuit is open;
  2. prompt: the versioned prompt file, checked against the schema version in code;
  3. per attempt: stage deadline check, budget reservation of the worst case (same transaction as the
     ai_calls 'started' row), provider call with timeout min(call timeout, time the stage has left);
  4. retry policy: one retry after a fast HTTP error (with backoff) or after invalid JSON (with the
     validation error appended, the "repair" attempt); no same-model retry after a timeout or refusal,
     go to the fallback instead; circuit breaker counts failures;
  5. validation of the output against the full JSON schema; two invalid outputs -> InvalidOutput.

Ported from the E2 gateway, with the recorded-response stub replaced by real providers (live Anthropic
SDK or the labelled simulator) and real prompts, payloads and token estimates.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

import jsonschema
from opentelemetry.trace import SpanKind, Status, StatusCode
from psycopg import errors as pg_errors

from .. import config
from ..settings import settings
from ..telemetry import tracer
from . import budget, circuit, prompts
from .errors import CallCancelled, CallFailed, InvalidOutput, NoIndependentModel, ProviderUnavailable, Refused, StageDeadline
from .providers import Provider, get_provider, provider_schema, render_user
from .schemas import SCHEMAS

_provider: Provider | None = None


def provider() -> Provider:
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


def set_provider(p: Provider | None) -> None:
    """Tests swap the provider (for example a fake Anthropic client)."""
    global _provider
    _provider = p


def recorded_name(model: str) -> str:
    """Model name written to ai_calls and to the report: simulated calls are prefixed 'sim:' so they can
    never be mistaken for real model output."""
    return model if settings.model_mode == "live" else f"sim:{model}"


def base_model(name: str | None) -> str | None:
    return name.removeprefix("sim:") if name else None


@dataclass
class StageCalls:
    """Shared by the calls of one stage execution: its deadline, a cancel switch (which also closes the
    stage's SDK client in live mode), and the calls in flight so the stage can account for any call
    thread that does not stop."""
    deadline: float                                   # time.monotonic() value
    cancel: threading.Event = field(default_factory=threading.Event)
    inflight: dict[int, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


@dataclass
class Completion:
    output: dict
    model: str                    # recorded name (with 'sim:' prefix in simulated mode)
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    fallback: bool


def route(role: str) -> dict:
    return config.routing()["roles"][role]


def _attempt(calls: StageCalls, report_id, purpose, role, model, prompt, user, payload, max_tokens, effort, timeout_cap):
    remaining = calls.remaining()
    if remaining < settings.llm_min_call_s:
        raise StageDeadline(f"{role}: {remaining:.1f} s left in the stage")
    if calls.cancel.is_set():
        raise CallCancelled()
    est_in = budget.estimate_tokens(prompt.text) + budget.estimate_tokens(user)
    reserved = budget.worst_case(model, est_in, max_tokens)
    name = recorded_name(model)
    call_id = budget.reserve(report_id, purpose, role, name, prompt.version, reserved)
    with calls.lock:
        calls.inflight[call_id] = reserved
    t0 = time.perf_counter()
    timeout = min(timeout_cap, remaining)
    with tracer.start_as_current_span(f"llm.call {role}", kind=SpanKind.CLIENT, record_exception=False,
                                      set_status_on_exception=False) as span:
        span.set_attributes({
            "gen_ai.operation.name": "chat", "gen_ai.provider.name": "simulated" if name.startswith("sim:") else "anthropic",
            "gen_ai.request.model": model, "gen_ai.request.max_tokens": max_tokens, "masader.role": role,
            "masader.report_id": report_id or "", "masader.purpose": purpose, "masader.prompt_version": prompt.version,
            "masader.ai_call_id": call_id, "masader.reserved_usd": float(reserved), "masader.timeout_s": float(timeout),
        })
        try:
            res = provider().call(model=model, role=role, system=prompt.text, user=user, payload=payload,
                                  schema=SCHEMAS[role], max_tokens=max_tokens, effort=effort, timeout_s=timeout,
                                  cancel=calls.cancel, report_id=report_id)
        except CallFailed as e:
            _span_outcome(span, e.kind)
            budget.settle(call_id, 0, 0, 0, int((time.perf_counter() - t0) * 1000), e.kind)
            if e.kind == "timeout" and timeout < timeout_cap:
                raise StageDeadline(f"{role}: {model} call reached the stage deadline") from e
            raise
        except Refused:
            _span_outcome(span, "refusal")
            budget.settle(call_id, 0, 0, 0, int((time.perf_counter() - t0) * 1000), "refusal")
            raise
        except CallCancelled:
            _span_outcome(span, "cancelled")
            budget.settle(call_id, 0, 0, 0, int((time.perf_counter() - t0) * 1000), "cancelled")
            raise
        finally:
            with calls.lock:
                calls.inflight.pop(call_id, None)
        ms = int((time.perf_counter() - t0) * 1000)
        cost = min(budget.call_cost(model, res.input_tokens, res.output_tokens), reserved)
        span.set_attributes({"gen_ai.response.model": model, "gen_ai.usage.input_tokens": res.input_tokens,
                             "gen_ai.usage.output_tokens": res.output_tokens, "masader.cost_usd": float(cost)})
        _span_outcome(span, "returned")
    return call_id, res, cost, ms


def _span_outcome(span, outcome: str) -> None:
    span.set_attribute("masader.outcome", outcome)
    if outcome not in ("returned", "ok", "fallback"):
        span.set_status(Status(StatusCode.ERROR, outcome))


def complete(role: str, payload: dict, *, calls: StageCalls, report_id: str | None = None, purpose: str = "live",
             exclude: tuple[str, ...] = (), timeout_s: float | None = None) -> Completion:
    """Run one role call. `exclude` holds base model names that must not be used (validator independence).
    Raises ProviderUnavailable, InvalidOutput, NoIndependentModel, BudgetExhausted, StageDeadline or
    CallCancelled; the stage maps each to an outcome (retry later, review, ...)."""
    with tracer.start_as_current_span(f"llm.complete {role}") as span:
        span.set_attributes({"masader.role": role, "masader.report_id": report_id or "", "masader.purpose": purpose,
                             "masader.excluded_models": ",".join(exclude)})
        try:
            done = _complete(role, payload, calls=calls, report_id=report_id, purpose=purpose, exclude=exclude,
                             timeout_s=timeout_s)
        except Exception as e:
            _span_outcome(span, type(e).__name__)
            raise
        span.set_attributes({"masader.model": done.model, "masader.fallback": done.fallback,
                             "masader.cost_usd": float(done.cost_usd)})
        _span_outcome(span, "fallback" if done.fallback else "ok")
        return done


def _complete(role: str, payload: dict, *, calls: StageCalls, report_id: str | None, purpose: str,
              exclude: tuple[str, ...], timeout_s: float | None) -> Completion:
    r = route(role)
    prompt = prompts.load(role, r["prompt"])
    user = render_user(role, payload)
    schema = SCHEMAS[role]
    timeout_cap = timeout_s or settings.llm_timeout_s
    chain = [m for m in (r["primary"], r.get("fallback")) if m]
    allowed = [m for m in chain if m not in exclude]
    for model in allowed:
        if circuit.is_open(model):
            continue
        invalid_seen, attempt_user = False, user
        for attempt in range(2):
            try:
                call_id, res, cost, ms = _attempt(calls, report_id, purpose, role, model, prompt, attempt_user, payload,
                                                  r["max_tokens"], r.get("effort"), timeout_cap)
            except CallFailed as e:
                circuit.record_failure(model)
                if e.kind == "timeout" or attempt == 1:
                    break                                  # timeout: a second wait rarely fits; 2nd error: go to fallback now
                if calls.cancel.wait(settings.llm_backoff_s):
                    raise CallCancelled() from e
                continue
            except Refused:
                break                                      # do not ask the same model again
            try:
                out = json.loads(res.text)
                jsonschema.validate(out, schema)
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                budget.settle(call_id, res.input_tokens, res.output_tokens, cost, ms, "invalid_json")
                if invalid_seen:
                    raise InvalidOutput(f"{role}: invalid output twice from {model}") from e
                invalid_seen = True                        # one repair attempt with the error message
                msg = e.msg if isinstance(e, json.JSONDecodeError) else e.message
                attempt_user = user + f"\n\n<repair>Your previous output was not valid against the schema: {msg[:300]}. " \
                                      f"Return only JSON that matches the schema.</repair>"
                continue
            circuit.record_success(model)
            fallback = model != chain[0]
            budget.settle(call_id, res.input_tokens, res.output_tokens, cost, ms, "fallback" if fallback else "ok")
            return Completion(out, recorded_name(model), prompt.version, res.input_tokens, res.output_tokens,
                              cost, ms, fallback)
    if len(allowed) < len(chain):
        raise NoIndependentModel(f"{role}: no model other than {', '.join(exclude)} is available")
    raise ProviderUnavailable(f"{role}: {', '.join(chain)} failed or circuit open")


def abandon(calls: StageCalls) -> list[int]:
    """For call threads that did not stop after cancellation: close their rows as 'cancelled' with the
    whole reservation booked as spent, before the process is replaced."""
    with calls.lock:
        ids = list(calls.inflight)
    for call_id in ids:
        try:
            budget.settle(call_id, 0, 0, 0, None, "cancelled")
        except pg_errors.Error:
            pass                                           # the reaper closes it as 'lost'
    return ids


def one_off(role: str, payload: dict, *, timeout_s: float, purpose: str = "live", report_id: str | None = None):
    """A single call outside a pipeline stage (the planner inside POST /v1/requests, 8 s budget)."""
    calls = StageCalls(deadline=time.monotonic() + timeout_s + settings.llm_min_call_s)
    try:
        return complete(role, payload, calls=calls, purpose=purpose, report_id=report_id, timeout_s=timeout_s)
    finally:
        provider().release(calls.cancel) if hasattr(provider(), "release") else None


__all__ = ["StageCalls", "Completion", "complete", "abandon", "one_off", "provider_schema", "base_model"]
