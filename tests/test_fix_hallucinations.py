import os
import sys
import tempfile

# config.py creates its folder on import: keep it out of the real profile.
_CONFIG_ROOT = tempfile.mkdtemp(prefix="notype-tests-")
os.environ["APPDATA"] = _CONFIG_ROOT
os.environ["XDG_CONFIG_HOME"] = _CONFIG_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from hallucinations import strip_credits  # noqa: E402


@pytest.mark.parametrize("text", [
    "字幕由Amara.org社区提供",
    "由Amara.org社区提供的字幕",
    "Untertitel der Amara.org-Community",
    "Napisy stworzone przez społeczność Amara.org",
])
def test_amara_credit_removed_completely(text):
    assert strip_credits(text) == ""


def test_amara_credit_after_real_sentence_keeps_the_sentence():
    assert strip_credits("Wir sehen uns morgen. Untertitel der Amara.org-Community") \
        == "Wir sehen uns morgen."


@pytest.mark.parametrize("text", [
    "Ich wohne in Samara.org ist keine Seite.",
    "Schau auf tamara.org nach.",
    "Die Datei heißt amara.org2.txt",
])
def test_amara_inside_other_words_left_alone(text):
    assert strip_credits(text) == text
