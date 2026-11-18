"""Origin grouping on the synthetic corpus against its ground truth (no database needed)."""
import datetime as dt
from itertools import combinations

from masader import config
from masader.connectors import fixture
from masader.ids import url_hash
from masader.processing.embed import DIM, HashingEmbedder
from masader.processing.origin_groups import ItemView, group
from masader.processing.text import content_hash


def views():
    rows = fixture.load()
    outlets = {s["code"]: s for s in config.sources()}
    emb = HashingEmbedder().embed([f"{r['title']}\n{r['body']}" for r in rows])
    out = []
    for i, (r, v) in enumerate(zip(rows, emb)):
        s = outlets[r["source"]]
        out.append(ItemView(i, s["outlet_name"], s.get("aliases", []), dt.datetime.fromisoformat(r["published"]),
                            f"{r['title']}\n{r['body']}", v, url_hash(r["url"]), content_hash(r["title"] + "\n" + r["body"])))
    return rows, out


def pair_scores():
    rows, items = views()
    res = group(items, config.thresholds()["dedup"])
    tp = fp = fn = 0
    for a, b in combinations(range(len(rows)), 2):
        same_true = rows[a]["truth_group"] == rows[b]["truth_group"]
        same_pred = res[a][0] == res[b][0]
        tp += same_true and same_pred
        fp += same_pred and not same_true
        fn += same_true and not same_pred
    return tp, fp, fn, res, rows


def test_embedder_is_normalised_and_deterministic():
    e = HashingEmbedder()
    a = e.embed(["Tanker transits fell 18 percent", "Tanker transits fell 18 percent", ""])
    assert a.shape == (3, DIM)
    assert abs(float(a[0] @ a[0]) - 1) < 1e-5 and float(a[0] @ a[1]) > 0.999 and float(a[2] @ a[2]) == 0


def test_no_false_merge_and_misses_are_cross_lingual():
    from masader.processing.text import detect_language

    tp, fp, fn, res, rows = pair_scores()
    assert fp == 0, "items from different stories were joined"
    missed = [(a, b) for a, b in combinations(range(len(rows)), 2)
              if rows[a]["truth_group"] == rows[b]["truth_group"] and res[a][0] != res[b][0]]
    lang = [detect_language(r["title"] + " " + r["body"]) for r in rows]
    assert all(lang[a] != lang[b] for a, b in missed), "a same-language copy was missed"


def test_each_grouping_method_fires():
    _, _, _, res, rows = pair_scores()
    methods = {m for _, m in res.values()}
    assert {"embedding", "attribution", "quote_set", "single"} <= methods


def test_cross_lingual_copy_is_a_known_miss():
    """The hashing embedder has no cross-lingual meaning: the Arabic version of the wire story is not
    joined to the English one (documented limitation; fastembed would be needed)."""
    _, _, _, res, rows = pair_scores()
    idx = {r["fixture_id"]: i for i, r in enumerate(rows)}
    assert res[idx["tanker-wire"]][0] != res[idx["tanker-wire-ar"]][0]
    assert res[idx["tanker-wire-ar"]][0] == res[idx["tanker-alkhaleej-ar"]][0]
