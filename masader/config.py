"""Versioned configuration loader (config/*.yaml) and the configuration hash stored with every report.

The hash covers active.yaml and every file it points to, plus the prompt files, so two reports with the
same hash were produced by the same prompts, models, rules and thresholds (FR13, architecture G.4).
"""
from __future__ import annotations

import functools
import hashlib
from pathlib import Path
from typing import Any

import yaml

from .settings import settings


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@functools.lru_cache(maxsize=1)
def active() -> dict[str, Any]:
    return _load(settings.config_dir / "active.yaml")


@functools.lru_cache(maxsize=1)
def routing() -> dict[str, Any]:
    return _load(settings.config_dir / active()["routing"])


@functools.lru_cache(maxsize=1)
def thresholds() -> dict[str, Any]:
    return _load(settings.config_dir / active()["thresholds"])


@functools.lru_cache(maxsize=1)
def sources() -> list[dict[str, Any]]:
    return _load(settings.config_dir / active()["sources"])["sources"]


@functools.lru_cache(maxsize=1)
def budgets() -> dict[str, Any]:
    return _load(settings.config_dir / "budgets.yaml")


@functools.lru_cache(maxsize=1)
def gazetteer() -> dict[str, list[str]]:
    return _load(settings.config_dir / "gazetteer.yaml")


@functools.lru_cache(maxsize=1)
def config_hash() -> str:
    """sha256 over active.yaml, the files it names and all prompt files, shortened to 12 hex chars."""
    h = hashlib.sha256()
    files = [settings.config_dir / "active.yaml"]
    files += [settings.config_dir / active()[k] for k in ("routing", "thresholds", "sources")]
    files += sorted(settings.prompts_dir.glob("*.md"))
    for f in files:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return "cfg-" + h.hexdigest()[:12]
