"""World Bank indicators API (IF-13) into the statistics table, with the publisher's "as of" date.
The fixture source loads data/sample/worldbank_fixture.json instead (values are NOT real World Bank data)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx

from ..store.db import pool
from .base import SourceFailed, get_with_retry


def parse_indicator(data) -> list[dict]:
    if not isinstance(data, list) or len(data) < 2 or data[1] is None:
        raise SourceFailed(f"unexpected World Bank answer: {str(data)[:120]}")
    meta, rows = data[0], data[1]
    updated = meta.get("lastupdated")
    out = []
    for r in rows:
        if r.get("value") is None:
            continue
        out.append({"indicator": r["indicator"]["id"], "country": r["countryiso3code"], "year": int(r["date"]),
                    "value": r["value"], "last_updated": updated})
    return out


def store_statistics(source_code: str, rows: list[dict]) -> int:
    with pool().connection() as c, c.transaction():
        sid = c.execute("SELECT id FROM sources WHERE code=%s", (source_code,)).fetchone()["id"]
        for r in rows:
            c.execute("INSERT INTO statistics (source_id, indicator, country, year, value, last_updated) "
                      "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                      (sid, r["indicator"], r["country"], r["year"], r["value"], r.get("last_updated")))
    return len(rows)


class WorldBankConnector:
    def __init__(self, source: dict, countries: list[str], client: httpx.Client | None = None, sleep=None):
        self.source, self.countries = source, countries
        self.client = client or httpx.Client()
        self.sleep = sleep

    def fetch_statistics(self) -> list[dict]:
        out = []
        year = dt.date.today().year
        kw = {"sleep": self.sleep} if self.sleep else {}
        for iso3 in self.countries:
            for ind in self.source.get("indicators", []):
                r = get_with_retry(self.client, f"{self.source['endpoint']}/country/{iso3}/indicator/{ind}",
                                   params={"format": "json", "date": f"{year - 6}:{year}"}, **kw)
                out += parse_indicator(r.json())
        return out


def load_fixture(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["rows"]
