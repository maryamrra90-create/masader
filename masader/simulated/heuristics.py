"""Deterministic heuristic simulator: stand-in outputs for each model role, computed from the real stage
payload. It is NOT a language model and is labelled as such everywhere it is used (MODEL_MODE=simulated,
model names recorded as "sim:<model>").

Why it exists: no Anthropic API key was available while the system was built, and the pipeline, the
evidence rules, the validator, the budget accounting and the failure handling all need realistic model
outputs to run end to end. Each function below follows the instructions of its prompt file in a crude,
rule-based way (pick sentences, match words and numbers, fill templates) and returns JSON that matches
the role's schema, so the rest of the system runs unchanged when MODEL_MODE=live.

What it means for results: anything measured with it (claims extracted, labels, support-check
detection) describes these heuristics, not Claude. The evaluation report says so next to each number.
"""
from __future__ import annotations

import re
from typing import Any

from ..processing.text import detect_language, normalise, numbers, sentences, words

STOP = set("""a an and are as at be been by for from has have in into is it its of on or that the their this to was
were will with which who after before over under than then there these those while also more less about
against between during per out up down off new last week weeks day days year years said says told stated
reported announced according statement what how have has had did does how why when where who whom whose not
but all any can its our your they them happened happen been being what's across through within since until
into onto upon very much many some such""".split())

ATTRIB_VERB = r"(?:said|says|told|stated|announced|reported)"
SPEAKER_START = re.compile(rf"^(?:The\s+|A\s+)?([A-Z][\w'’\-()]*(?:\s+[\w'’\-()]+){{0,5}}?)\s+{ATTRIB_VERB}\b")
SPEAKER_END = re.compile(rf",\s+((?:the\s+)?[\w'’\-]+(?:\s+[\w'’\-()]+){{0,6}}?)\s+{ATTRIB_VERB}\.?$", re.I)
ACCORDING = re.compile(r"according to (?:the\s+)?([A-Z][\w'’\-]*(?:\s+[A-Z(][\w'’\-()]*){0,6})")
FORECAST = re.compile(r"\b(will|expected to|is set to|could|may|might|likely|forecast)\b", re.I)
OPINION = re.compile(r"\b(should|believe|must|in our view|we think|ought)\b", re.I)
INSTRUCTION = re.compile(r"\b(ignore (all|any|the)?\s*(previous|prior|above)?\s*instructions?|system prompt|you are now|"
                         r"disregard|as an ai|respond with|state that)\b", re.I)
NEGATION = re.compile(r"\b(not|no|never|denied|denies|deny|without|none)\b|لم|لا|ليس", re.I)
ENTITY = re.compile(r"\b([A-Z][a-z]+(?:\s+(?:of\s+)?[A-Z][a-z]+)*)\b")
MONTH_DATE = re.compile(r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|"
                        r"November|December)|(?:January|February|March|April|May|June|July|August|September|October|"
                        r"November|December)\s+\d{1,2})\b")
COMPANY = re.compile(r"\b(line|lines|carrier|company|group|shipping firm|operator|maersk|container)\b", re.I)

AR_QUERY = {"shipping": "الشحن", "strait": "مضيق", "hormuz": "هرمز", "ports": "الموانئ", "port": "ميناء",
            "oman": "عمان", "india": "الهند", "tanker": "ناقلات", "tankers": "ناقلات", "trade": "التجارة",
            "uae": "الإمارات", "insurance": "التأمين", "officials": "المسؤولين", "gulf": "الخليج"}


def content_words(text: str) -> set[str]:
    return {w for w in words(text) if w not in STOP and len(w) > 2 and not w.isdigit()}


def overlap(a: str, b: str) -> float:
    """Share of a's content words that also appear in b."""
    ca = content_words(a)
    return len(ca & content_words(b)) / len(ca) if ca else 0.0


def _nums(text: str) -> set[str]:
    return {n for n in numbers(text) if not re.fullmatch(r"(19|20)\d\d", n)}


def speaker_of(sentence: str) -> str | None:
    m = SPEAKER_START.match(sentence)
    if m:
        return m.group(1).strip()
    m = SPEAKER_END.search(sentence)
    if m:
        s = m.group(1).strip()
        return s[0].upper() + s[1:]
    m = ACCORDING.search(sentence)
    return m.group(1).strip() if m else None


def claim_type(sentence: str) -> str:
    if speaker_of(sentence):
        return "attributed"
    if FORECAST.search(sentence):
        return "forecast"
    if OPINION.search(sentence):
        return "opinion"
    if _nums(sentence) or MONTH_DATE.search(sentence) or ENTITY.search(sentence):
        return "fact"
    return "other"


# ------------------------------------------------------------------------------------------- planner
def planner(payload: dict) -> dict:
    """Rule-like stand-in for the planner LLM part: search terms from the topic and questions, and the
    one domain question the proposal's example asks (statements by shipping companies)."""
    req = payload["request"]
    text = " ".join([req.get("topic", "")] + req.get("questions", []))
    terms = [w for w in words(text) if w not in STOP and len(w) > 3 and not w.isdigit()]
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    queries_en = [" ".join(seen[:4])] + seen[:10]
    queries_ar = [AR_QUERY[t] for t in seen if t in AR_QUERY][:6]
    questions = []
    prefer = (req.get("sources") or {}).get("prefer") or []
    if re.search(r"\bshipping\b", text, re.I) and not prefer and not req.get("entities"):
        questions.append({"id": "q1", "text": "Include statements by shipping companies, or official sources and news only?",
                          "default": "include", "options": ["include", "official_and_news_only"]})
    return {"too_broad": False, "ambiguous_entities": [], "questions": questions,
            "queries_en": queries_en[:11], "queries_ar": queries_ar}


# ------------------------------------------------------------------------------------------- extractor
def _score_sentence(s: str, query_words: set[str]) -> float:
    score = 0.0
    if _nums(s):
        score += 2
    if speaker_of(s):
        score += 1.5
    if MONTH_DATE.search(s):
        score += 0.5
    score += 1.0 * len(content_words(s) & query_words)
    return score


def extractor(payload: dict) -> dict:
    """Pick the most claim-like sentences of each item (numbers, attributed statements, words of the
    request) and copy each one verbatim as the passage, as the prompt requires. Sentences that address
    the model (prompt-injection attempts) are skipped, as the prompt instructs."""
    req = payload.get("request", {})
    query_words = content_words(" ".join([req.get("topic", "")] + req.get("questions", [])))
    claims: list[dict] = []
    for item in payload.get("items", []):
        cand = []
        for s in sentences(item["text"]):
            if len(s) < 30 or len(s) > 400 or INSTRUCTION.search(s) or not re.search(r"[.!?؟\"”]$", s):
                continue                              # too short or long, an injection attempt, or a headline
            if not (content_words(s) & query_words):
                continue
            cand.append((_score_sentence(s, query_words), s))
        cand.sort(key=lambda x: -x[0])
        for _, s in cand[:3]:
            claims.append({
                "text": s, "type": claim_type(s), "speaker": speaker_of(s),
                "numbers": sorted(_nums(s)), "dates": MONTH_DATE.findall(s),
                "entities": sorted({e for e in ENTITY.findall(s) if e.lower() not in STOP})[:8],
                "item_id": item["id"], "passage": s})
    return {"claims": claims[:8]}


# ------------------------------------------------------------------------------------------- verifier
def best_sentence(claim_text: str, item_text: str) -> tuple[float, str]:
    best = (0.0, "")
    for s in sentences(item_text):
        o = overlap(claim_text, s)
        if o > best[0]:
            best = (o, s)
    return best


def verifier(payload: dict) -> dict:
    """For each (claim, item) pair: find the item sentence that shares most content words with the claim.
    Enough overlap and matching figures -> supports; the same subject with different figures, or a
    negation on one side only -> contradicts; otherwise not_addressed."""
    claims = {c["id"]: c for c in payload["claims"]}
    items = {i["id"]: i for i in payload["items"]}
    out = []
    for cid, iid in payload["pairs"]:
        c, it = claims[cid], items[iid]
        ov, sent = best_sentence(c["text"], it["text"])
        verdict, reason = "not_addressed", "no passage on the claim's subject"
        if ov >= 0.5:
            cn, sn = _nums(c["text"]), _nums(sent)
            neg_c, neg_s = bool(NEGATION.search(c["text"])), bool(NEGATION.search(sent))
            if cn and sn and not (cn & sn):
                verdict, reason = "contradicts", f"states {', '.join(sorted(sn))} where the claim states {', '.join(sorted(cn))}"
            elif neg_c != neg_s and ov >= 0.6:
                verdict, reason = "contradicts", "the passage negates the claim"
            elif cn and not sn:
                verdict, reason = "not_addressed", "same subject but no figure to compare"
            else:
                verdict, reason = "supports", f"passage states the same ({int(ov * 100)}% word overlap)"
        outlet = it.get("outlet", "")
        first_hand = bool(re.search(r"press office|ministry|directorate|statistics|world bank", outlet, re.I))
        out.append({"claim_id": cid, "item_id": iid, "verdict": verdict, "reason": reason, "first_hand": first_hand})
    return {"results": out}


# ------------------------------------------------------------------------------------------- writer
def _attributed(c: dict) -> str:
    """Claim text with the attribution the evidence rules require."""
    t = c["text"].rstrip(".")
    if c.get("attribution_required") and not re.search(rf"\b(according to|{ATTRIB_VERB[3:-1]})\b", t, re.I):
        t += f", according to {c.get('outlet') or 'one source'}"
    return t + "."


def _repeats(c: dict, claims: list[dict], used: set) -> bool:
    """The same fact (same figures, many shared words) is already in the brief from another item."""
    return any(overlap(c["text"], u["text"]) >= 0.4 and _nums(c["text"]) == _nums(u["text"])
               for u in claims if u["id"] in used)


def writer(payload: dict) -> dict:
    """Template brief from the labelled claims: corroborated and attributed facts as findings, figures
    in by_the_numbers, both versions for disputed claims, attributed statements as actors, and
    forecasts in what_to_watch. Every sentence cites only the claims it copies from."""
    # the simulator cannot translate, so it leaves out claims written in Arabic (a real writer translates them)
    claims = [c for c in payload.get("claims", []) if c.get("label") != "insufficient"
              and detect_language(c["text"]) != "ar"]
    if payload.get("rewrite"):
        by_id = {c["id"]: c for c in payload.get("claims", [])}
        sections: dict[str, list] = {k: [] for k in ("key_findings", "by_the_numbers", "disputed", "actors",
                                                      "analysis", "what_to_watch")}
        for r in payload["rewrite"]:
            usable = [by_id[i] for i in r["cites"] if i in by_id and by_id[i].get("label") != "insufficient"]
            if not usable:
                continue                                  # nothing safe to say: drop the sentence
            if r["section"] == "disputed" and len(usable) >= 2:
                text = f"{_attributed(usable[0])[:-1]}; another source gives a different account: {_attributed(usable[1])}"
            else:
                text = _attributed(usable[0])
            sections.setdefault(r["section"], []).append({"text": text, "cites": [c["id"] for c in usable[:2]]})
        return {"sections": sections}

    def rank(c):
        return ({"corroborated": 0, "single_source": 1, "disputed": 2}.get(c.get("label"), 3), -len(c.get("numbers", [])))

    facts = sorted([c for c in claims if c["type"] in ("fact", "attributed") and c["label"] != "disputed"
                    and not c.get("low_reliability")], key=rank)
    key_findings, used = [], set()
    for c in facts:
        if len(key_findings) == 6:
            break
        if FORECAST.search(c["text"]) and not re.search(r"\b(said|according to|stated|told)\b", c["text"], re.I):
            continue
        if _repeats(c, claims, used):
            continue                                  # the same fact from another item: one sentence is enough
        key_findings.append({"text": _attributed(c), "cites": [c["id"]]})
        used.add(c["id"])
    numbers_ = []
    for c in sorted(claims, key=rank):
        if (len(numbers_) < 5 and c.get("numbers") and c["id"] not in used and c["label"] != "disputed"
                and not c.get("low_reliability") and not FORECAST.search(c["text"]) and not _repeats(c, claims, used)):
            numbers_.append({"text": _attributed(c), "cites": [c["id"]]})
            used.add(c["id"])
    disputed, seen_pairs = [], set()
    for c in claims:
        if c["label"] != "disputed":
            continue
        others = [o for o in claims if o["id"] in c.get("versions", [])]
        if not others:
            continue
        o = others[0]
        pair = frozenset((c["id"], o["id"]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        disputed.append({"text": f"{_attributed(c)[:-1]}; another source gives a different account: {_attributed(o)}",
                         "cites": [c["id"], o["id"]]})
    actors = [{"text": _attributed(c), "cites": [c["id"]]} for c in claims
              if c["type"] == "attributed" and c.get("speaker") and c["id"] not in used and c["label"] != "disputed"][:4]
    analysis = []
    if len(key_findings) >= 2:
        a, b = key_findings[0]["cites"][0], key_findings[1]["cites"][0]
        ca = next(c for c in claims if c["id"] == a)
        cb = next(c for c in claims if c["id"] == b)
        analysis.append({"text": f"Read together, two checked findings bear on the request: {ca['text'].rstrip('.')}, "
                                 f"and {cb['text'][0].lower() + cb['text'][1:].rstrip('.')}.", "cites": [a, b]})
    watch = [{"text": f"Whether this materialises: {c['text'].rstrip('.')}.", "cites": [c["id"]]}
             for c in claims if c["type"] == "forecast"][:3]
    return {"sections": {"key_findings": key_findings, "by_the_numbers": numbers_, "disputed": disputed,
                         "actors": actors, "analysis": analysis, "what_to_watch": watch}}


# ------------------------------------------------------------------------------------------- splitter
SPLIT_SECTIONS = ("key_findings", "by_the_numbers", "disputed", "actors", "analysis")


def splitter(payload: dict) -> dict:
    """One assertion per sentence of the checkable sections (what_to_watch is worded as possibilities and
    is not checked), with the sentence's citations and the person it quotes, if any."""
    out, n = [], 0
    for section in SPLIT_SECTIONS:
        for s in payload["sections"].get(section, []):
            n += 1
            m = SPEAKER_START.match(s["text"])
            out.append({"n": n, "section": section, "text": s["text"], "cites": s["cites"],
                        "speaker": m.group(1).strip() if m else None})
    return {"assertions": out}


# ------------------------------------------------------------------------------------------- support check
OPPOSITES = [("rose", "fell"), ("rise", "fall"), ("increased", "decreased"), ("up", "down"), ("raised", "cut"),
             ("higher", "lower"), ("resumed", "halted"), ("opened", "closed"), ("open", "closed"), ("gained", "lost"),
             ("more", "fewer"), ("expanded", "shrank"), ("lifted", "imposed"), ("approved", "rejected")]


def _direction_flip(text: str, evidence: str) -> bool:
    """A direction word in the assertion whose opposite, and not itself, is in the evidence."""
    tw, ew = set(words(text)), set(words(evidence))
    for a, b in OPPOSITES:
        for x, y in ((a, b), (b, a)):
            if x in tw and x not in ew and y in ew:
                return True
    return False


FRAMING = content_words("according another source gives different account read together two checked findings "
                        "bear request whether materialises one")


def support(payload: dict) -> dict:
    """Lexical support check: an assertion fails when any of its figures, or more than 30% of its content
    words (template framing words excluded), are absent from the cited claims and passages, when it
    negates what the evidence says, or when it states the opposite direction (rose / fell ...)."""
    out = []
    for a in payload["assertions"]:
        ev = " ".join(a.get("evidence", []))
        aw = content_words(a["text"]) - FRAMING
        missing = aw - content_words(ev)
        share = len(missing) / len(aw) if aw else 0.0
        extra_nums = _nums(a["text"]) - _nums(ev)
        neg_flip = bool(NEGATION.search(a["text"])) and not NEGATION.search(ev)
        if extra_nums:
            out.append({"n": a["n"], "result": "fail", "reason": f"figures not in the evidence: {sorted(extra_nums)}"})
        elif neg_flip:
            out.append({"n": a["n"], "result": "fail", "reason": "negates the cited evidence"})
        elif _direction_flip(a["text"], ev):
            out.append({"n": a["n"], "result": "fail", "reason": "states the opposite direction to the evidence"})
        elif share > 0.30:
            out.append({"n": a["n"], "result": "fail", "reason": f"words not in the evidence: {sorted(missing)[:6]}"})
        else:
            out.append({"n": a["n"], "result": "pass",
                        "reason": f"all figures in the evidence; {len(missing)} of {len(aw)} content words absent "
                                  f"({share:.0%}, tolerance 30%)"})
    return {"results": out}


ROLES = {"planner": planner, "extractor": extractor, "verifier": verifier, "writer": writer,
         "splitter": splitter, "support": support}


def run(role: str, payload: dict) -> dict[str, Any]:
    return ROLES[role](payload)


__all__ = ["run", "content_words", "overlap", "best_sentence", "speaker_of", "claim_type", "normalise"]
