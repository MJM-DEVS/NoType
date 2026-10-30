import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import _robustness_stubs  # noqa: E402

_STUB_NAMES = ("faster_whisper", "faster_whisper.utils", "faster_whisper.vad",
               "sounddevice", "correction_watch", "output_mute", "escape_hook",
               "context_capture")


def test_foreign_stub_is_swapped_for_the_import_and_put_back(monkeypatch):
    """Another test file's SimpleNamespace as text_output lacks what backend
    imports: replaced while backend imports, then back in place. Our own
    stubs don't stay in sys.modules."""
    foreign = SimpleNamespace(type_text=lambda text: None)
    monkeypatch.setitem(sys.modules, "text_output", foreign)
    for name in ("backend", "transcriber", "audio_recorder") + _STUB_NAMES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    before = {n for n in _STUB_NAMES if n in sys.modules}

    backend = _robustness_stubs.import_backend()

    assert callable(backend.insert_text)
    assert sys.modules["text_output"] is foreign
    assert {n for n in _STUB_NAMES if n in sys.modules} == before


def test_foreign_stub_with_needed_names_is_used(monkeypatch):
    foreign = SimpleNamespace(insert_text=lambda *a, **k: True,
                              copy_selection=lambda *a, **k: "")
    monkeypatch.setitem(sys.modules, "text_output", foreign)
    for name in ("backend", "transcriber", "audio_recorder"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    backend = _robustness_stubs.import_backend()

    assert backend.insert_text is foreign.insert_text
    assert sys.modules["text_output"] is foreign
