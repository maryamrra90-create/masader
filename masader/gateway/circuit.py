"""Circuit breaker per model, with its state in Redis so all worker processes share it (B.3.6):
5 consecutive failures open the circuit for 60 s; a success resets the count."""
from __future__ import annotations

import time

from ..redisclient import rds
from ..settings import settings


def is_open(model: str) -> bool:
    until = rds().get(f"cb:open:{model}")
    return until is not None and float(until) > time.time()


def record_failure(model: str) -> None:
    n = rds().incr(f"cb:fail:{model}")
    rds().expire(f"cb:fail:{model}", 120)
    if n >= settings.cb_threshold:
        rds().set(f"cb:open:{model}", time.time() + settings.cb_open_s, px=int(settings.cb_open_s * 1000))
        rds().delete(f"cb:fail:{model}")


def record_success(model: str) -> None:
    rds().delete(f"cb:fail:{model}")
