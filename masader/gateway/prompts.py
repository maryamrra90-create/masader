"""Versioned prompt files (prompts/<role>.<lang>.v<major>.<minor>.md) with a front-matter header.
The gateway refuses a prompt whose declared role or schema version does not match the code (D.3)."""
from __future__ import annotations

import functools
from dataclasses import dataclass

import yaml

from ..settings import settings
from .schemas import SCHEMA_VERSION


class PromptError(Exception):
    """The prompt file is missing or does not match the role or the schema version in code."""


@dataclass(frozen=True)
class Prompt:
    role: str
    version: str
    text: str


@functools.lru_cache(maxsize=32)
def load(role: str, version: str) -> Prompt:
    path = settings.prompts_dir / f"{version}.md"
    if not path.exists():
        raise PromptError(f"prompt file {path.name} not found")
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise PromptError(f"{path.name}: missing front matter")
    _, header, body = raw.split("---\n", 2)
    meta = yaml.safe_load(header)
    if meta.get("role") != role:
        raise PromptError(f"{path.name}: declares role {meta.get('role')!r}, expected {role!r}")
    if str(meta.get("schema_version")) != SCHEMA_VERSION:
        raise PromptError(f"{path.name}: schema version {meta.get('schema_version')} != code {SCHEMA_VERSION}")
    if meta.get("version") != version:
        raise PromptError(f"{path.name}: version header {meta.get('version')!r} does not match the file name")
    return Prompt(role, version, body.strip())
