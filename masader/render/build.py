"""Assembling the report body (ReportOut, architecture C.3) from the stored pipeline outputs, and the
HTML rendering with Jinja2 autoescaping (template brief.en.html.j2, version 1.0)."""
from __future__ import annotations

import datetime as dt
import functools
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE = "brief.en.html.j2"
TEMPLATE_VERSION = "1.0"
SECTION_TITLES = {"key_findings": "Key findings", "by_the_numbers": "By the numbers", "disputed": "Disputed",
                  "actors": "Actors and positions", "analysis": "Analysis", "what_to_watch": "What to watch",
                  "evidence_gaps": "Evidence gaps"}
LABEL_TEXT = {"corroborated": "Corroborated", "single_source": "Single source", "disputed": "Disputed",
              "insufficient": "Insufficient evidence"}


@functools.lru_cache(maxsize=1)
def env() -> Environment:
    e = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                    autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)
    e.filters["day"] = lambda s: dt.datetime.fromisoformat(s).strftime("%d %b %Y") if s else ""
    return e


def render_html(body: dict) -> str:
    return env().get_template(TEMPLATE).render(r=body, titles=SECTION_TITLES, label_text=LABEL_TEXT)
