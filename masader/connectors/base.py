"""Connector interface and the HTTP helper every live connector uses (architecture IF-12, IF-13):
20 s timeout, 3 retries with 2, 4 and 8 s backoff on timeouts, 429 and 5xx; then the source is skipped
and the failure noted, never retried in a loop."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from ..processing.ingest import RawItem

log = logging.getLogger("masader.connectors")
USER_AGENT = "Masader/1.0 (research brief generator; course project)"


class SourceFailed(Exception):
    """The source could not be read after the retries; the caller notes it and moves on."""


class Connector(Protocol):
    code: str

    def fetch(self) -> list[RawItem]: ...


def get_with_retry(client: httpx.Client, url: str, *, params: dict | None = None, headers: dict | None = None,
                   retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = client.get(url, params=params, headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=20.0)
            if r.status_code == 304:                     # not modified (conditional GET)
                return r
            if r.status_code == 429 or r.status_code >= 500:
                last = SourceFailed(f"HTTP {r.status_code}")
            else:
                r.raise_for_status()
                return r
        except httpx.HTTPStatusError as e:
            raise SourceFailed(f"HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            last = e
        if attempt < retries:
            sleep(2 ** (attempt + 1))
    raise SourceFailed(f"{url}: {last}")
