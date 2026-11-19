import dataclasses
import json

import httpx
import pytest

from masader import settings as S
from masader.connectors import base, gdelt, rss, worldbank
from masader.ids import canonical_hash, new_api_key, new_id, url_hash
from masader.render.build import render_html

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Port &amp; ships</title><link>https://x.example/a</link><description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
<pubDate>Fri, 16 Oct 2026 09:40:00 +0400</pubDate></item>
<item><title>No date</title><link>https://x.example/b</link></item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="https://y.example/1"/>
<summary>S</summary><updated>2026-10-16T05:40:00Z</updated></entry></feed>"""


def test_rss_and_atom_parsing():
    items, skipped = rss.parse_feed("src", RSS)
    assert skipped == 1 and items[0].title == "Port & ships" and items[0].text == "Hello world"
    assert items[0].published.utcoffset().total_seconds() == 4 * 3600
    a, _ = rss.parse_feed("src", ATOM)
    assert a[0].url == "https://y.example/1"
    with pytest.raises(base.SourceFailed):
        rss.parse_feed("src", b"not xml")


def test_rss_refuses_entity_expansion_and_external_entities():
    """Feeds are untrusted: a 'billion laughs' bomb or an XXE reference is refused, never expanded."""
    bomb = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            b"<rss><channel><item><title>&b;</title></item></channel></rss>")
    xxe = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           b"<rss><channel><item><title>&x;</title></item></channel></rss>")
    for body in (bomb, xxe):
        with pytest.raises(base.SourceFailed, match="unsafe XML"):
            rss.parse_feed("src", body)


def _client(responses):
    seq = iter(responses)

    def handler(request):
        r = next(seq)
        return r(request) if callable(r) else r
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retry_then_success_and_conditional_get():
    sleeps = []
    c = _client([httpx.Response(503), httpx.Response(200, content=RSS, headers={"ETag": "v1"}), httpx.Response(304)])
    con = rss.RSSConnector({"code": "s", "endpoint": "https://x.example/feed"}, client=c, sleep=sleeps.append)
    assert len(con.fetch()) == 1 and sleeps == [2] and con.etag == "v1"
    assert con.fetch() == []


def test_retries_are_bounded():
    sleeps = []
    c = _client([httpx.Response(429)] * 4)
    with pytest.raises(base.SourceFailed):
        base.get_with_retry(c, "https://x.example", sleep=sleeps.append)
    assert sleeps == [2, 4, 8]
    with pytest.raises(base.SourceFailed):
        base.get_with_retry(_client([httpx.Response(404)]), "https://x.example", sleep=sleeps.append)


def test_gdelt_parsing_and_text_errors():
    data = {"articles": [{"url": "https://n.example/1", "title": "T", "seendate": "20261016T054000Z", "language": "English"},
                         {"url": "", "title": "x", "seendate": "20261016T054000Z"}, {"title": "bad date", "url": "u", "seendate": "x"}]}
    items = gdelt.parse_artlist("gdelt.doc", data)
    assert len(items) == 1 and items[0].lang == "en"
    con = gdelt.GDELTConnector({"code": "g", "endpoint": "https://api.example/doc"}, "q",
                               client=_client([httpx.Response(200, text="Timespan too short")]), sleep=lambda s: None)
    with pytest.raises(base.SourceFailed):
        con.fetch()


def test_worldbank_parsing():
    data = [{"lastupdated": "2026-07-01"}, [{"indicator": {"id": "NY.GDP.MKTP.CD"}, "countryiso3code": "OMN", "date": "2025",
                                              "value": 1.0e11}, {"indicator": {"id": "X"}, "countryiso3code": "OMN", "date": "2024",
                                                                 "value": None}]]
    rows = worldbank.parse_indicator(data)
    assert rows == [{"indicator": "NY.GDP.MKTP.CD", "country": "OMN", "year": 2025, "value": 1.0e11, "last_updated": "2026-07-01"}]
    with pytest.raises(base.SourceFailed):
        worldbank.parse_indicator([{"message": "invalid"}])
    c = _client([httpx.Response(200, json=data)])
    con = worldbank.WorldBankConnector({"endpoint": "https://wb.example/v2", "indicators": ["NY.GDP.MKTP.CD"]}, ["OMN"], client=c)
    assert len(con.fetch_statistics()) == 1


def test_settings_refuse_unsafe_configurations():
    s = S.settings
    with pytest.raises(ValueError, match="timer order"):
        S.check(dataclasses.replace(s, llm_timeout_s=300))
    with pytest.raises(ValueError, match="cancel wait"):
        S.check(dataclasses.replace(s, cancel_wait_s=40))
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        S.check(dataclasses.replace(s, model_mode="live", anthropic_api_key=None))
    with pytest.raises(ValueError, match="KEY_PEPPER"):
        S.check(dataclasses.replace(s, env="prod"))
    with pytest.raises(ValueError, match="MODEL_MODE"):
        S.check(dataclasses.replace(s, model_mode="mock"))


def test_ids_and_hashes():
    assert new_id("rep_").startswith("rep_") and len(new_id("rep_")) == 16
    assert new_api_key("reviewer").startswith("rv_")
    assert url_hash("HTTPS://X.example/a/?utm_source=t&b=1#frag") == url_hash("https://x.example/a?b=1")
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_html_render_escapes_source_text():
    body = {"report_id": "rep_x", "version": 1, "title": "<script>alert(1)</script>", "questions": ["q"],
            "window": {"from": "2026-10-12", "to": "2026-10-19"}, "generated_at": "2026-10-20T00:00:00+00:00",
            "review_status": "not_reviewed", "publication": "private", "statistics": [],
            "sections": {"key_findings": [{"text": "<img src=x onerror=alert(1)>", "findings": ["f1"]}], "evidence_gaps": []},
            "findings": [{"id": "f1", "label": "corroborated", "text": "t", "reliability": {"reason": "r", "low_reliability": False},
                          "sources": [{"outlet": "o", "tier": "A", "published": "2026-10-16T09:40:00+04:00",
                                       "url": "javascript:alert(1)", "passage": "<b>p</b>", "first_hand": False,
                                       "verdict": "supports"}]}],
            "method": {"rule_version": "rules-1.1.1", "simulated": True}, "versions": {}, "notes": []}
    html = render_html(body)
    assert "<script>alert" not in html and "&lt;img" in html and "&lt;b&gt;p" in html
    assert "Simulated model output" in html
    json.dumps(body)
