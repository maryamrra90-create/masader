"""Loader for the SYNTHETIC sample corpus (data/sample/corpus.jsonl). Every outlet in it is fictional
("(fixture)" in its name, .example domain); it exists so the system can be run, tested and demonstrated
without network access to real sources. Each line also carries the ground-truth origin group used by the
dedup evaluation (`truth_group`), which the system itself never reads."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from ..processing.ingest import RawItem
from ..settings import ROOT

# SAMPLE_DIR lets an installed package (the Docker image) find the data outside site-packages
SAMPLE = Path(os.environ.get("SAMPLE_DIR", str(ROOT / "data" / "sample")))
CORPUS = SAMPLE / "corpus.jsonl"
WORLDBANK = SAMPLE / "worldbank_fixture.json"


def load(path: Path = CORPUS) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def raw_items(path: Path = CORPUS) -> list[RawItem]:
    return [RawItem(r["source"], r["url"], r["title"], r["body"], dt.datetime.fromisoformat(r["published"]))
            for r in load(path)]
