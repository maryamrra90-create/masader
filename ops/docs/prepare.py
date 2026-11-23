"""Builds docs/index.md for the documentation site from README.md, so the README stays the single source.

Links that point into docs/ are made relative to the site; links to code, config or other folders are
pointed at the file on GitHub. Run by `make docs` and by .github/workflows/docs.yml before `mkdocs build`.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = "https://github.com/maryamrra90-create/masader/blob/main/"


def convert(text: str) -> str:
    def fix(m: re.Match[str]) -> str:
        label, target = m.group(1), m.group(2)
        if re.match(r"^(https?:|#|mailto:)", target):
            return m.group(0)
        if target.startswith("docs/"):
            return f"{label}({target[len('docs/'):]})"
        return f"{label}({REPO}{target})"

    return re.sub(r"(!?\[[^\]]*\])\(([^)\s]+)\)", fix, text)


def main() -> None:
    src = (ROOT / "README.md").read_text(encoding="utf-8")
    note = "<!-- generated from README.md by ops/docs/prepare.py; do not edit -->\n"
    (ROOT / "docs" / "index.md").write_text(note + convert(src), encoding="utf-8")


if __name__ == "__main__":
    main()
