"""GDELT DOC 2.0 article list (IF-12): discovery only. GDELT returns links and titles; the finding cites
the linked article, tiered by its outlet. At most one request per 5 seconds."""
from __future__ import annotations

import datetime as dt
import threading
import time

import httpx

from ..processing.ingest import RawItem
from .base import SourceFailed, get_with_retry

_lock = threading.Lock()
_last = [0.0]
MIN_INTERVAL_S = 5.0


def parse_artlist(code: str, data: dict) -> list[RawItem]:
    out = []
    for a in data.get("articles", []):
        try:
            published = dt.datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
        except (KeyError, ValueError):
            continue
        if not a.get("url") or not a.get("title"):
            continue
        lang = {"English": "en", "Arabic": "ar"}.get(a.get("language", ""), None)
        out.append(RawItem(code, a["url"], a["title"], a["title"], published, lang))
    return out


class GDELTConnector:
    def __init__(self, source: dict, query: str, timespan: str = "7d", client: httpx.Client | None = None, sleep=None):
        self.code, self.url, self.query, self.timespan = source["code"], source["endpoint"], query, timespan
        self.client = client or httpx.Client()
        self.sleep = sleep or time.sleep

    def fetch(self) -> list[RawItem]:
        with _lock:                                   # politeness: <= 1 request / 5 s per process
            wait = _last[0] + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            _last[0] = time.monotonic()
        r = get_with_retry(self.client, self.url, sleep=self.sleep,
                           params={"query": self.query, "mode": "artlist", "format": "json",
                                   "maxrecords": 250, "timespan": self.timespan})
        try:
            data = r.json()
        except ValueError as e:                       # GDELT answers errors as plain text with HTTP 200
            raise SourceFailed(f"non-JSON answer: {r.text[:120]}") from e
        return parse_artlist(self.code, data)
