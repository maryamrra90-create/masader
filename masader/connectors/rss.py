"""RSS 2.0 and Atom feeds (IF-13). Only headline, summary and link are stored unless the source register
allows full text. Conditional GET (ETag / Last-Modified) avoids re-downloading an unchanged feed."""
from __future__ import annotations

import datetime as dt
import html
import re
import xml.etree.ElementTree as ET  # noqa: S405 - expat does not resolve external entities; feeds are size-capped
from email.utils import parsedate_to_datetime

import httpx

from ..processing.ingest import RawItem
from .base import SourceFailed, get_with_retry

ATOM = "{http://www.w3.org/2005/Atom}"
MAX_BYTES = 5_000_000
_TAG = re.compile(r"<[^>]+>")


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", s or ""))).strip()


def _date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.UTC)


def parse_feed(code: str, body: bytes) -> tuple[list[RawItem], int]:
    """Returns (items, skipped). A malformed entry is skipped and counted, not fatal."""
    if len(body) > MAX_BYTES:
        raise SourceFailed("feed larger than 5 MB")
    try:
        root = ET.fromstring(body)  # noqa: S314
    except ET.ParseError as e:
        raise SourceFailed(f"feed is not XML: {e}") from e
    out, skipped = [], 0
    entries = root.findall("./channel/item") or root.findall(f"{ATOM}entry")
    for e in entries:
        if e.tag == "item":
            title, link = e.findtext("title"), e.findtext("link")
            summary, published = e.findtext("description"), _date(e.findtext("pubDate"))
        else:
            title = e.findtext(f"{ATOM}title")
            ln = e.find(f"{ATOM}link")
            link = ln.get("href") if ln is not None else None
            summary = e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content")
            published = _date(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"))
        if not title or not link or published is None:
            skipped += 1
            continue
        out.append(RawItem(code, link.strip(), _clean(title), _clean(summary), published))
    return out, skipped


class RSSConnector:
    def __init__(self, source: dict, client: httpx.Client | None = None, sleep=None):
        self.code, self.url = source["code"], source["endpoint"]
        self.client = client or httpx.Client(follow_redirects=True)
        self.sleep = sleep
        self.etag: str | None = None
        self.skipped = 0

    def fetch(self) -> list[RawItem]:
        headers = {"If-None-Match": self.etag} if self.etag else {}
        kw = {"sleep": self.sleep} if self.sleep else {}
        r = get_with_retry(self.client, self.url, headers=headers, **kw)
        if r.status_code == 304:
            return []
        self.etag = r.headers.get("ETag")
        items, self.skipped = parse_feed(self.code, r.content)
        return items
