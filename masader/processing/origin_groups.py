"""Exact and near-duplicate detection and origin grouping (FR5, test U10, architecture D.2 step 4).

Items are joined into one origin group when any of these holds (union-find over all pairs in the window):
  1. exact duplicate: same normalised URL hash or same content hash;
  2. embedding cosine >= 0.85 and published within +/-3 days;
  3. Jaccard >= 0.6 on 5-word shingles (exact Jaccard; MinHash is unnecessary for a few hundred items);
  4. attribution: the text credits another outlet in the window ("Gulf Wire Service reported",
     "according to ..."), and that outlet has an item on the same story (shared figures or cosine >= 0.3)
     within one day;
  5. two or more identical direct quotations of at least 8 words (copies of one press release).
Origin groups, not outlets, are what the evidence rules count.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import numpy as np

from .text import jaccard, normalise, numbers, quotations, shingles

ATTRIBUTION = re.compile(r"(according to|reported|told|said in a statement|citing|cited|quoted|نقلت|ذكرت|أفادت|وفقا|وفقاً|حسب)",
                         re.I)


@dataclass
class ItemView:
    id: int
    outlet: str                       # outlet name as in the source register
    aliases: list[str]                # other names the outlet is credited by (e.g. its Arabic name)
    published: dt.datetime
    text: str                         # title + body
    embedding: np.ndarray
    url_hash: bytes
    content_hash: bytes
    _shingles: set = field(default=None, repr=False)   # type: ignore[assignment]
    _quotes: set = field(default=None, repr=False)     # type: ignore[assignment]
    _numbers: set = field(default=None, repr=False)    # type: ignore[assignment]

    def __post_init__(self):
        self._shingles = shingles(self.text)
        self._quotes = quotations(self.text)
        self._numbers = {n for n in numbers(self.text) if not re.fullmatch(r"(19|20)\d\d", n)}   # years are not evidence of one story


class _UnionFind:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}
        self.method: dict[int, str] = {}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b, method):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        root, child = min(ra, rb), max(ra, rb)
        self.parent[child] = root
        self.method.setdefault(root, method)


def _credited_outlets(item: ItemView, outlets: dict[str, list[str]]) -> set[str]:
    """Outlets (other than the item's own) that the item's text credits with an attribution verb nearby."""
    text = normalise(item.text)
    credited = set()
    for outlet, names in outlets.items():
        if outlet == item.outlet:
            continue
        for name in names:
            n = normalise(name)
            for m in re.finditer(re.escape(n), text):
                window = text[max(0, m.start() - 60): m.end() + 60]
                if ATTRIBUTION.search(window):
                    credited.add(outlet)
                    break
    return credited


def group(items: list[ItemView], cfg: dict) -> dict[int, tuple[int, str]]:
    """Return {item_id: (root_item_id, method)}; items with the same root form one origin group.
    method is how the group was first formed ('single' for a group of one)."""
    uf = _UnionFind([i.id for i in items])
    by_url: dict[bytes, int] = {}
    by_content: dict[bytes, int] = {}
    for it in items:
        for index, key in ((by_url, it.url_hash), (by_content, it.content_hash)):
            if key in index:
                uf.union(index[key], it.id, "embedding")       # exact copies (recorded as the closest method)
            else:
                index[key] = it.id

    window = dt.timedelta(days=cfg["day_window"])
    emb = np.stack([i.embedding for i in items]) if items else np.zeros((0, 1))
    sims = emb @ emb.T if len(items) else emb
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            ia, ib = items[a], items[b]
            if abs(ia.published - ib.published) > window:
                continue
            if sims[a, b] >= cfg["cosine_near_duplicate"]:
                uf.union(ia.id, ib.id, "embedding")
            elif jaccard(ia._shingles, ib._shingles) >= cfg["jaccard_shingles"]:
                uf.union(ia.id, ib.id, "minhash")
            elif len(ia._quotes & ib._quotes) >= cfg["shared_quotes_min"]:
                uf.union(ia.id, ib.id, "quote_set")

    outlets: dict[str, list[str]] = {}
    for it in items:
        outlets.setdefault(it.outlet, [it.outlet.replace(" (fixture)", "")] + it.aliases)
    index = {it.id: n for n, it in enumerate(items)}
    for it in items:
        for outlet in _credited_outlets(it, outlets):
            best, best_score = None, 0.0
            for other in items:
                if other.outlet != outlet or abs(other.published - it.published) > dt.timedelta(days=1):
                    continue
                shared = len(it._numbers & other._numbers)
                score = float(sims[index[it.id], index[other.id]]) + 0.5 * shared
                if (shared or sims[index[it.id], index[other.id]] >= 0.3) and score > best_score:
                    best, best_score = other, score
            if best is not None:
                uf.union(it.id, best.id, "attribution")

    out = {}
    for it in items:
        root = uf.find(it.id)
        out[it.id] = (root, uf.method.get(root, "single"))
    return out
