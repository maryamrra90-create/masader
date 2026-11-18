"""Shared Redis client (broker connection is Celery's own). Short timeouts so an outage surfaces fast."""
from __future__ import annotations

import functools

import redis

from .settings import settings


@functools.lru_cache(maxsize=1)
def rds() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, socket_timeout=1.0, socket_connect_timeout=1.0,
                                decode_responses=True)
