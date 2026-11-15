import json

import pytest

from agent_webhook import MAX_SHOWN_CHARS, extract_message

JSON = "application/json"


def js(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


@pytest.mark.parametrize("obj, expected", [
    ({"message": "Erledigt"}, "Erledigt"),
    ({"reply": "Okay"}, "Okay"),
    ({"text": "Gespeichert"}, "Gespeichert"),
    ({"text": "t", "reply": "r", "message": "m"}, "m"),
    ({"message": "", "reply": "r"}, "r"),
    ({"message": "   ", "text": "t"}, "t"),
    ({"message": 42, "reply": "r"}, "r"),
    ({"message": {"nested": "x"}, "text": "t"}, "t"),
])
def test_json_object_field_order(obj, expected):
    assert extract_message(js(obj), JSON) == expected


@pytest.mark.parametrize("obj", [{}, {"status": "ok"}, {"message": None}, {"message": ["a"]}])
def test_json_object_without_answer_field_shows_sent(obj):
    assert extract_message(js(obj), JSON) == ""


def test_json_object_parsed_whatever_the_content_type():
    assert extract_message(js({"message": "hi"}), "text/plain") == "hi"


def test_json_string():
    assert extract_message(js("Fertig"), JSON) == "Fertig"


@pytest.mark.parametrize("body", [b"[1, 2]", b"42", b"true"])
def test_other_json_values_show_sent(body):
    assert extract_message(body, "application/json; charset=utf-8") == ""


@pytest.mark.xfail(strict=False, raises=AssertionError, reason=(
    "Bug: json.loads('null') is None, which the code reads as 'not JSON', so "
    "the overlay shows the literal 'null' while [] or 42 show 'Sent'"))
def test_json_null_shows_sent():
    assert extract_message(b"null", JSON) == ""


def test_plain_text():
    assert extract_message(b"  Alles klar  ", "text/plain") == "Alles klar"


def test_plain_text_becomes_one_line():
    assert extract_message(b"Zeile 1\nZeile 2\r\n\tZeile 3", "text/plain") == "Zeile 1 Zeile 2 Zeile 3"


def test_long_text_cut_with_ellipsis():
    out = extract_message(b"x" * 500, "text/plain")
    assert len(out) == MAX_SHOWN_CHARS
    assert out.endswith("…")


def test_text_exactly_at_limit_not_cut():
    assert extract_message(b"y" * MAX_SHOWN_CHARS, "text/plain") == "y" * MAX_SHOWN_CHARS


def test_long_json_message_cut_too():
    out = extract_message(js({"message": "wort " * 100}), JSON)
    assert len(out) <= MAX_SHOWN_CHARS and out.endswith("…")


def test_html_tags_removed():
    body = b"<html><body><h1>Danke</h1><p>Eintrag <b>gespeichert</b></p></body></html>"
    assert extract_message(body, "text/html; charset=utf-8") == "Danke Eintrag gespeichert"


def test_html_tags_kept_for_plain_text():
    assert extract_message(b"<b>x</b>", "text/plain") == "<b>x</b>"


@pytest.mark.xfail(strict=False, raises=AssertionError, reason=(
    "Bug (minor): HTML entities are not decoded, the overlay shows '&amp;'"))
def test_html_entities_decoded():
    assert extract_message(b"<p>Fish &amp; Chips</p>", "text/html") == "Fish & Chips"


@pytest.mark.parametrize("body", [b"", b"   \n\t "])
def test_empty_body_shows_sent(body):
    assert extract_message(body, "text/plain") == ""


def test_charset_from_header():
    assert extract_message("Grüße".encode("latin-1"), "text/plain", "latin-1") == "Grüße"


def test_unknown_charset_falls_back_to_utf8():
    assert extract_message("Grüße".encode("utf-8"), "text/plain", "no-such-charset") == "Grüße"


def test_invalid_bytes_replaced_not_raised():
    assert extract_message(b"ok \xff\xfe", "text/plain").startswith("ok ")


def test_missing_content_type():
    assert extract_message(b"Hallo") == "Hallo"
    assert extract_message(js({"reply": "r"})) == "r"
