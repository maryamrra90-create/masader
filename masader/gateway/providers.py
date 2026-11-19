"""Model providers behind one interface. Roles never import a provider SDK; only the gateway does.

* AnthropicProvider (MODEL_MODE=live): the official Anthropic Python SDK, Messages API with a JSON-schema
  structured output and a cached system prompt. It is unit-tested against a fake SDK client only: no API
  key was available while this was built, so it has NOT been run against the real API.
* SimulatedProvider (MODEL_MODE=simulated, the default): returns outputs from the deterministic heuristic
  simulator in masader/simulated, computed from the real stage input. It exists so the whole system can
  run, be tested and be demonstrated without an API key. Its outputs are not model outputs, and any
  quality number measured with it describes the simulator, not Claude.

Both support fault injection for drills through Redis keys (simulated provider only):
    fault:<model>               = timeout | error | invalid_json | invalid_json_once | hang | hang_uncancellable | refusal
    fault:r:<report_id>:<role>  = a list of the same values for one report; each call takes one
"""
from __future__ import annotations

import copy
import html
import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..redisclient import rds
from ..settings import settings
from ..simulated import heuristics
from .budget import estimate_tokens
from .errors import CallCancelled, CallFailed, Refused

# Appendix A6 time budget per role in minutes (used only for the simulated latency)
SIM_MINUTES = {"planner": 0.1, "extractor": 1.5, "verifier": 1.5, "writer": 1.5, "splitter": 0.5, "support": 0.5}


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int


class Provider(Protocol):
    def call(self, *, model: str, role: str, system: str, user: str, payload: dict, schema: dict,
             max_tokens: int, effort: str | None, timeout_s: float, cancel: threading.Event,
             report_id: str | None) -> ProviderResult: ...


# ---------------------------------------------------------------- rendering the user message
def _attr(v: Any) -> str:
    return html.escape(str(v), quote=True)


def _source_block(item: dict) -> str:
    """Source text goes inside a tagged data block; '<' and '>' are escaped so a source cannot close the
    block or open a fake one (prompt-injection control, architecture D.3)."""
    return (f'<source id="{item["id"]}" outlet="{_attr(item["outlet"])}" tier="{item["tier"]}" '
            f'published="{_attr(item["published"])}">\n{html.escape(item["text"], quote=False)}\n</source>')


def render_user(role: str, payload: dict) -> str:
    parts: list[str] = []
    if "request" in payload:
        parts.append(f"<request>{html.escape(json.dumps(payload['request'], ensure_ascii=False), quote=False)}</request>")
    if "questions" in payload:
        parts.append("<questions>\n" + "\n".join(f"- {html.escape(q)}" for q in payload["questions"]) + "\n</questions>")
    for c in payload.get("claims", []):
        parts.append(f'<claim id="{c["id"]}">{html.escape(json.dumps(c, ensure_ascii=False), quote=False)}</claim>')
    for item in payload.get("items", []):
        parts.append(_source_block(item))
    if "pairs" in payload:
        parts.append(f"<pairs>{json.dumps(payload['pairs'])}</pairs>")
    if "sections" in payload:
        parts.append(f"<report>{html.escape(json.dumps(payload['sections'], ensure_ascii=False), quote=False)}</report>")
    for a in payload.get("assertions", []):
        parts.append(f'<assertion n="{a["n"]}">{html.escape(json.dumps(a, ensure_ascii=False), quote=False)}</assertion>')
    if payload.get("rewrite"):
        parts.append(f"<rewrite>{html.escape(json.dumps(payload['rewrite'], ensure_ascii=False), quote=False)}</rewrite>")
    return "\n\n".join(parts)


def provider_schema(schema: dict) -> dict:
    """Copy of the schema without keywords that structured outputs may not accept (array bounds) and with
    nullable types written as anyOf. The full schema is still enforced locally after the call."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("maxItems", None)
            node.pop("minItems", None)
            t = node.get("type")
            if isinstance(t, list):
                node.pop("type")
                node["anyOf"] = [{"type": x} for x in t]
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    return s


# ---------------------------------------------------------------- live provider
class AnthropicProvider:
    """One SDK client per stage execution, closed by the stage's cancel switch so calls waiting on the
    network return at once (architecture B.3.4)."""

    def __init__(self, client_factory=None):
        self._factory = client_factory or self._default_client
        self._clients: dict[int, Any] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_client():  # pragma: no cover - needs a real key
        import anthropic  # noqa: PLC0415 - imported only in live mode

        return anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0)

    def client_for(self, cancel: threading.Event):
        key = id(cancel)
        with self._lock:
            if key not in self._clients:
                client = self._factory()
                self._clients[key] = client
                threading.Thread(target=self._closer, args=(cancel, key), daemon=True).start()
            return self._clients[key]

    def _closer(self, cancel: threading.Event, key: int) -> None:
        cancel.wait()
        with self._lock:
            client = self._clients.pop(key, None)
        if client is not None:
            client.close()

    def release(self, cancel: threading.Event) -> None:
        """Called when a stage finishes normally: close its client without cancelling anything."""
        cancel.set()

    def call(self, *, model, role, system, user, payload, schema, max_tokens, effort, timeout_s, cancel, report_id):
        import anthropic  # noqa: PLC0415

        if cancel.is_set():
            raise CallCancelled()
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": provider_schema(schema)}}
        if effort and not model.startswith("claude-haiku-4"):        # Haiku 4.5 does not take effort
            output_config["effort"] = effort
        try:
            resp = self.client_for(cancel).with_options(timeout=timeout_s).messages.create(
                model=model, max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config=output_config,
            )
        except anthropic.APITimeoutError as e:
            if cancel.is_set():
                raise CallCancelled() from e
            raise CallFailed("timeout", str(e)) from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            if cancel.is_set():
                raise CallCancelled() from e
            raise CallFailed("error", type(e).__name__) from e
        usage = resp.usage
        tin = (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        if resp.stop_reason == "refusal":
            raise Refused(f"{role}: {model} declined the request")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        return ProviderResult(text, tin, usage.output_tokens or 0)


# ---------------------------------------------------------------- simulated provider
class SimulatedProvider:
    def _fault(self, model: str, role: str, report_id: str | None) -> str | None:
        r = rds()
        if report_id:
            f = r.lpop(f"fault:r:{report_id}:{role}")
            if f:
                return f
        f = r.get(f"fault:{model}")
        if f == "invalid_json_once" and r.delete(f"fault:{model}"):
            f = "invalid_json"
        return f

    def release(self, cancel: threading.Event) -> None:
        cancel.set()

    def call(self, *, model, role, system, user, payload, schema, max_tokens, effort, timeout_s, cancel, report_id):
        fault = self._fault(model, role, report_id)
        if fault == "hang_uncancellable":
            time.sleep(3600)
        if fault == "hang":
            cancel.wait(3600)
            raise CallCancelled()
        if fault == "error":
            raise CallFailed("error", "injected provider error")
        if fault == "refusal":
            raise Refused(f"{role}: injected refusal")
        # Simulated latency jitter only; nothing security-relevant uses this random number.
        latency = SIM_MINUTES.get(role, 0.5) * settings.sim_latency_scale * random.uniform(0.8, 1.2)  # nosec B311
        wait = timeout_s if fault == "timeout" else latency
        if wait > 0 and cancel.wait(min(wait, timeout_s)):
            raise CallCancelled()
        if fault == "timeout" or wait >= timeout_s:
            raise CallFailed("timeout", "injected timeout")
        tin = estimate_tokens(system) + estimate_tokens(user)
        if fault == "invalid_json":
            return ProviderResult('{"claims": [ {"text": unterminated', tin, 50)
        out = heuristics.run(role, payload)
        text = json.dumps(out, ensure_ascii=False)
        return ProviderResult(text, tin, estimate_tokens(text))


def get_provider() -> Provider:
    return AnthropicProvider() if settings.model_mode == "live" else SimulatedProvider()
