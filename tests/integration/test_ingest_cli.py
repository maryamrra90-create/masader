"""Ingest idempotency, stored origin groups, retrieval, World Bank statistics and the CLI."""
import datetime as dt
import json

import pytest

from masader import cli
from masader.processing import ingest, retrieve
from masader.processing.ingest import RawItem

pytestmark = pytest.mark.db


def test_seeded_corpus_is_grouped(db):
    with db.connection() as c:
        n = c.execute("SELECT count(*) AS n FROM items").fetchone()["n"]
        g = c.execute("SELECT count(*) AS n FROM items WHERE origin_group_id IS NULL").fetchone()["n"]
        st = c.execute("SELECT count(*) AS n FROM statistics").fetchone()["n"]
    assert n == 37 and g == 0 and st == 12


def test_ingest_is_idempotent(db):
    from masader.connectors import fixture
    assert ingest.ingest(fixture.raw_items()) == []


def test_a_later_copy_joins_the_stored_group_and_unregistered_sources_are_skipped(db):
    with db.connection() as c:
        g = c.execute("SELECT origin_group_id FROM items WHERE url LIKE %s", ("%/jebelali-wire",)).fetchone()["origin_group_id"]
    text = ("Jebel Ali port reported normal operations this week, with container volumes steady at about 290,000 TEU.\n\n"
            "The port operator said vessel waiting times were unchanged.")
    new = ingest.ingest([RawItem("fx.newsroundup.en", "https://newsroundup.example/news/2026/jebel-copy",
                                 "Jebel Ali reports normal operations", text, dt.datetime(2026, 10, 17, 12, tzinfo=dt.UTC)),
                         RawItem("not.registered", "https://x.example/1", "t", "t", dt.datetime(2026, 10, 17, tzinfo=dt.UTC))])
    assert len(new) == 1
    with db.connection() as c:
        assert c.execute("SELECT origin_group_id FROM items WHERE id=%s", (new[0],)).fetchone()["origin_group_id"] == g
        c.execute("DELETE FROM items WHERE id=%s", (new[0],))


def test_retrieval_is_windowed_and_capped(db):
    rows = retrieve.candidates(["tanker transits hormuz"], [], dt.date(2026, 10, 12), dt.date(2026, 10, 19))
    with db.connection() as c:
        urls = {r["id"]: r["url"] for r in c.execute("SELECT id, url FROM items")}
    got = [urls[r["id"]].rsplit("/", 1)[1] for r in rows]
    assert "tanker-wire" in got and "old-hormuz" not in got and "later-hormuz" not in got
    assert not any(g.startswith("noise-") for g in got)
    capped = retrieve.cap_per_group([dict(r) for r in rows], 1, 5)
    assert len(capped) <= 5 and len({r["origin_group_id"] for r in capped}) == len(capped)
    assert retrieve.hit_count(["hormuz"], dt.date(2026, 10, 12), dt.date(2026, 10, 19)) >= 10


def test_cli_commands(db, capsys):
    assert cli.main(["key", "create", "CLI user", "cli@example.org", "reviewer"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["api_key"].startswith("rv_")
    assert cli.main(["key", "revoke", out["key_id"]]) == 0
    assert json.loads(capsys.readouterr().out) == {"revoked": True}
    assert cli.main(["migrate"]) == 0 and json.loads(capsys.readouterr().out) == {"applied": []}
    assert cli.main(["seed"]) == 0
    assert cli.main(["delete-user", "nobody@example.org"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"keys": 0}
