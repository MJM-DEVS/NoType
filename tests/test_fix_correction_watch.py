import ctypes
import os
import sys
import tempfile
import types

# config.py creates its folder on import: keep it out of the real profile.
_CONFIG_ROOT = tempfile.mkdtemp(prefix="notype-tests-")
os.environ["APPDATA"] = _CONFIG_ROOT
os.environ["XDG_CONFIG_HOME"] = _CONFIG_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# correction_watch reads ctypes.windll at import; only find_corrections() is
# tested here, which never touches it.
if not hasattr(ctypes, "windll"):
    ctypes.windll = types.SimpleNamespace(user32=None)

import pytest  # noqa: E402

from correction_watch import find_corrections  # noqa: E402


def fix(inserted, after, prefix="", suffix=""):
    """find_corrections() for a field that held prefix + inserted + suffix."""
    return find_corrections(inserted, prefix + inserted + suffix, after)


@pytest.mark.parametrize("inserted, after", [
    ("Kuber Netis läuft", "Kubernetes läuft"),
    ("Kuber Netis", "Kubernetes"),
])
def test_multi_word_fix_in_short_dictation(inserted, after):
    assert fix(inserted, after) == [("Kuber Netis", "Kubernetes")]


@pytest.mark.parametrize("inserted, after", [
    ("Hallo Welt", "Ganz Anderes"),
    ("Hallo Welt wie geht es", ""),
    ("Hallo Welt wie geht es", "Hallo"),
    ("Kuber Netis", "Docker"),
])
def test_short_dictation_mostly_gone_still_none(inserted, after):
    assert fix(inserted, after) is None


def test_lowercase_rewrite_of_short_dictation_still_none():
    assert fix("wie geht es", "die geht es") == []


@pytest.mark.parametrize("after", [
    "Wir nutzen Kubernetes. Und noch mehr Text danach",
    "Wir nutzen Kubernetes und noch mehr Text danach",
    "Wir nutzen Kubernetes und so",
])
def test_fix_of_last_word_then_typing_on(after):
    assert fix("Wir nutzen Kuber Netis", after) == [("Kuber Netis", "Kubernetes")]


def test_fix_of_last_word_then_typing_on_before_unchanged_suffix():
    assert fix("Wir nutzen Kuber Netis", "Wir nutzen Kubernetes und Helm. Gruß Max",
               suffix=". Gruß Max") == [("Kuber Netis", "Kubernetes")]


def test_multi_word_meant_at_the_end():
    assert fix("Wir fliegen nach Nju Jork", "Wir fliegen nach New York") \
        == [("Nju Jork", "New York")]


@pytest.mark.parametrize("after", [
    "Wir nutzen heute etwas ganz anderes",
    "Wir nutzen Docker und noch mehr Text danach",
    "Wir nutzen Kuber Netis und noch mehr Text danach",
])
def test_typing_on_without_a_fix_gives_nothing(after):
    assert fix("Wir nutzen Kuber Netis", after) == []


def test_fix_in_the_middle_unchanged():
    assert fix("Wir nutzen Kuber Netis heute", "Wir nutzen Kubernetes heute") \
        == [("Kuber Netis", "Kubernetes")]
