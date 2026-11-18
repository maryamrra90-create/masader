from masader.processing import redact as R
from masader.processing import text as T


def test_normalise_and_passage_check_tolerate_spacing_quotes_and_digits():
    assert T.passage_in("fell  18 percent", "Tanker transits FELL 18 percent in a week")
    assert T.passage_in("“open, safe”", 'ports remain "open, safe" and')
    assert T.passage_in("١٨ بالمئة", "انخفض 18 بالمئة خلال أسبوع")
    assert not T.passage_in("fell 28 percent", "fell 18 percent")
    assert not T.passage_in("", "anything")


def test_language_detection_by_script():
    assert T.detect_language("Tanker transits through Hormuz fell") == "en"
    assert T.detect_language("تراجع عبور الناقلات في مضيق هرمز") == "ar"
    assert T.detect_language("123 ...") == "other"


def test_sentences_break_on_lines_and_full_stops():
    assert T.sentences("Headline here\n\nFirst one. Second one.") == ["Headline here", "First one.", "Second one."]


def test_paragraph_packing_respects_limit():
    text = " ".join(f"Sentence number {i} is here." for i in range(100))
    paras = T.paragraphs(text, 200)
    assert all(len(p) <= 200 for p in paras) and len(paras) > 5


def test_shingles_jaccard_quotes_numbers():
    a, b = T.shingles("one two three four five six"), T.shingles("one two three four five seven")
    assert 0 < T.jaccard(a, b) < 1 and T.jaccard(set(), a) == 0
    q = T.quotations('He said "members are maintaining their published schedules while monitoring" today')
    assert q == {"members are maintaining their published schedules while monitoring"}
    assert T.numbers("rose 0.5 percent and ٤ ships") == ["0.5", "4"]


def test_redaction_of_personal_data():
    r = R.redact("Mail ali@example.com or call +971 50 123 4567, ID 784-1990-1234567-1, card 4111 1111 1111 1111")
    assert "ali@example.com" not in r.text and "[EMAIL]" in r.text
    assert "[PHONE]" in r.text and "[ID_NUMBER]" in r.text and "[CARD]" in r.text
    assert set(r.found) >= {"EMAIL", "PHONE", "ID_NUMBER", "CARD"}


def test_card_number_needs_luhn():
    assert "[CARD]" not in R.redact("order 1234 5678 9012 3456").text


def test_input_policy():
    assert R.check_policy("api key sk-ant-api03-AbCdEf0123456789abcdef0123456789") is not None
    assert R.check_policy("CONFIDENTIAL: internal memo on shipping") is not None
    assert R.check_policy("Find the home address of my neighbour") is not None
    assert R.check_policy("restricted shipping lanes in the Strait of Hormuz") is None
    assert R.check_policy("حركة الشحن السريع في الخليج") is None
    assert R.check_policy("وثيقة سري للغاية") is not None
