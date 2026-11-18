"""Exceptions raised by the gateway (architecture B.3.6). The pipeline maps each one to a stage outcome."""


class GatewayError(Exception):
    pass


class ProviderUnavailable(GatewayError):
    """Every model for the role failed or has an open circuit; the stage waits and retries."""


class InvalidOutput(GatewayError):
    """The model output failed schema validation twice; the report goes to review."""


class NoIndependentModel(GatewayError):
    """The support check would have to run on the writer's model; a human reviews instead."""


class BudgetExhausted(GatewayError):
    """The worst-case cost of the next attempt does not fit under the purpose's cap."""


class StageDeadline(GatewayError):
    """Too little of the stage's time is left to start another attempt."""


class CallCancelled(GatewayError):
    """The stage cancelled its outstanding calls (time limit, or a sibling call failed)."""


class Refused(GatewayError):
    """The model declined the request (stop_reason refusal); not retried on the same model."""


class CallFailed(GatewayError):
    """One attempt failed (timeout, HTTP error); the gateway decides whether to retry or fall back."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind          # "timeout" | "error"
