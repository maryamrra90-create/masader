"""Celery tasks: one task per stage (masader.stage.<stage>), the reaper and the ingest tasks (IF-14).
Each stage is its own task so a worker crash loses one stage, not the whole report."""
from __future__ import annotations

import logging

from celery import chain
from celery.exceptions import Ignore

from ..settings import settings
from .celery_app import app
from .kit import STAGES, execute_stage, reap
from .stages import collect, dedup, extract, label, render, validate, verify, write

log = logging.getLogger("masader.tasks")
FUNCS = {"collect": collect.run, "dedup": dedup.run, "extract": extract.run, "verify": verify.run,
         "label": label.run, "write": write.run, "validate": validate.run, "render": render.run}


def make_stage(stage: str):
    @app.task(name=f"masader.stage.{stage}", bind=True, max_retries=20)
    def run(self, report_id: str):
        outcome = execute_stage(stage, report_id, FUNCS[stage])
        if outcome == "done":
            return report_id
        if outcome == "retry":
            raise self.retry(countdown=settings.stage_retry_s)
        if outcome == "provider_wait":
            raise self.retry(countdown=settings.provider_retry_s)
        raise Ignore()                     # 'skip' (duplicate message) or 'stop' (review / failed)
    return run


TASKS = {s: make_stage(s) for s in STAGES}


def start_pipeline(report_id: str, from_stage: str = "collect"):
    i = STAGES.index(from_stage)
    return chain(*[TASKS[s].si(report_id) for s in STAGES[i:]]).apply_async()


@app.task(name="masader.ops.reaper")
def reaper():
    r = reap()
    for report_id, stage in r["restart"]:
        start_pipeline(report_id, stage)
    if r["lost_calls"] or r["restart"]:
        log.warning("reaper: %s", r)
    return {k: v for k, v in r.items() if k != "restart"} | {"restarted": len(r["restart"])}


@app.task(name="masader.ingest.fixture")
def ingest_fixture():
    from ..connectors import fixture, worldbank
    from ..processing.ingest import ingest, sync_sources

    sync_sources()
    n = len(ingest(fixture.raw_items()))
    worldbank.store_statistics("fx.worldbank.stats", worldbank.load_fixture(fixture.WORLDBANK))
    return n


def _live(kind: str):
    from .. import config
    return [s for s in config.sources() if s["kind"] == kind and s.get("active") and "endpoint" in s]


@app.task(name="masader.ingest.rss")
def ingest_rss():                          # pragma: no cover - needs outbound access to the feeds
    from ..connectors.base import SourceFailed
    from ..connectors.rss import RSSConnector
    from ..processing.ingest import ingest

    n = 0
    for s in _live("rss"):
        try:
            n += len(ingest(RSSConnector(s).fetch()))
        except SourceFailed as e:
            log.warning("source %s skipped: %s", s["code"], e)
    return n


@app.task(name="masader.ingest.gdelt")
def ingest_gdelt(query: str = "(Hormuz OR shipping) sourcelang:english"):   # pragma: no cover - needs network
    from ..connectors.base import SourceFailed
    from ..connectors.gdelt import GDELTConnector
    from ..processing.ingest import ingest

    n = 0
    for s in _live("gdelt"):
        try:
            n += len(ingest(GDELTConnector(s, query).fetch()))
        except SourceFailed as e:
            log.warning("source %s skipped: %s", s["code"], e)
    return n


@app.task(name="masader.ingest.worldbank")
def ingest_worldbank(countries=("OMN", "ARE", "IND", "SAU", "QAT")):       # pragma: no cover - needs network
    from ..connectors.base import SourceFailed
    from ..connectors.worldbank import WorldBankConnector, store_statistics

    n = 0
    for s in _live("worldbank"):
        try:
            n += store_statistics(s["code"], WorldBankConnector(s, list(countries)).fetch_statistics())
        except SourceFailed as e:
            log.warning("source %s skipped: %s", s["code"], e)
    return n
