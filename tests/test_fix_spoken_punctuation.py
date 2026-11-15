import os
import sys
import tempfile

# config.py creates its folder on import: keep it out of the real profile.
_CONFIG_ROOT = tempfile.mkdtemp(prefix="notype-tests-")
os.environ["APPDATA"] = _CONFIG_ROOT
os.environ["XDG_CONFIG_HOME"] = _CONFIG_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from spoken_punctuation import apply  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("No comma thanks", "No, thanks"),
    ("Thank you for your time exclamation mark", "Thank you for your time!"),
    ("This is just a test question mark", "This is just a test?"),
    ("Please give notice new line Thanks", "Please give notice\nThanks"),
])
def test_period_only_literals_do_not_block_other_commands(text, expected):
    assert apply(text, "en") == expected


@pytest.mark.parametrize("text", [
    "the trial period",
    "We have a grace period",
    "for a short time period",
    "Write it with no period",
    "the comma is here",
    "a new line",
])
def test_literal_words_still_keep_period_and_article_phrases(text):
    assert apply(text, "en") == text


def test_colon_after_period_only_literal_is_a_command():
    assert apply("Reporting colon new line Revenue is up", "en") == "Reporting:\nRevenue is up"


def test_full_stop_after_period_only_literal_stays_literal():
    assert apply("We need a test full stop", "en") == "We need a test full stop"


def test_period_after_period_only_literal_before_another_command():
    # Whisper set "period" apart: still a command after "test".
    assert apply("This is a test. Period. New paragraph Bye", "en") == "This is a test.\n\nBye"
