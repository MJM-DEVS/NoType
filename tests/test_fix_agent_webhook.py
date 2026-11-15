import os
import sys
import tempfile

# config.py creates its folder on import: keep it out of the real profile.
_CONFIG_ROOT = tempfile.mkdtemp(prefix="notype-tests-")
os.environ["APPDATA"] = _CONFIG_ROOT
os.environ["XDG_CONFIG_HOME"] = _CONFIG_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from agent_webhook import extract_message  # noqa: E402

JSON = "application/json"


@pytest.mark.parametrize("body", [b"null", b" null\n", b"[]", b"42", b"true"])
def test_json_non_text_values_show_sent(body):
    assert extract_message(body, JSON) == ""


def test_json_string_still_shown():
    assert extract_message(b'"Fish &amp; Chips"', JSON) == "Fish &amp; Chips"


def test_null_as_plain_text_is_text():
    assert extract_message(b"null", "text/plain") == "null"


def test_html_entities_decoded():
    assert extract_message(b"<p>Fish &amp; Chips &lt;3 &#8364;</p>", "text/html") \
        == "Fish & Chips <3 €"


def test_html_decoded_entities_are_not_tags():
    # Decoded after tag removal: an escaped tag is shown, not dropped.
    assert extract_message(b"<p>&lt;b&gt;x&lt;/b&gt;</p>", "text/html") == "<b>x</b>"


def test_plain_text_entities_kept():
    assert extract_message(b"Fish &amp; Chips", "text/plain") == "Fish &amp; Chips"
