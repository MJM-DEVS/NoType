import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

import gc  # noqa: E402
import weakref  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import _robustness_stubs  # noqa: E402

transcriber = _robustness_stubs.import_transcriber()
Transcriber = transcriber.Transcriber


class FakeModel:
    """Stands in for WhisperModel; records the transcribe() arguments."""

    def __init__(self, source, device=None, compute_type=None):
        self.source = source
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter([]), SimpleNamespace(language="de", language_probability=1.0)


@pytest.fixture(autouse=True)
def fake_whisper(monkeypatch):
    monkeypatch.setattr(transcriber, "WhisperModel", FakeModel)
    monkeypatch.setattr(Transcriber, "_local_model_path", lambda self: None)
    Transcriber._model_cache.clear()
    yield
    Transcriber._model_cache.clear()


def _loaded(size):
    t = Transcriber(model_size=size, device="cpu")
    t.load_model()
    return t


def test_cache_keeps_only_one_model():
    small = _loaded("small")
    ref = weakref.ref(small._model)
    del small
    medium = _loaded("medium")
    assert list(Transcriber._model_cache) == [("medium", "cpu", "int8")]
    gc.collect()
    assert ref() is None, "the old model must be released"
    # same model again: a cache hit, no reload
    again = _loaded("medium")
    assert again._model is medium._model


@pytest.mark.parametrize("setting, expected", [
    ("auto", 1), ("1", 1), (5, 5), ("3", 3),
    ("abc", 1), (None, 1), ("0", 1), (-2, 1), ("99", 1), ([5], 1),
])
def test_beam_size_validated(setting, expected):
    t = Transcriber(model_size="small", device="cpu")
    assert t._get_beam_size(2.0, setting) == expected


def test_invalid_config_values_do_not_fail_transcription():
    t = _loaded("small")
    result = t.transcribe(np.zeros(16000, dtype=np.float32), languages=[("de", 1.0)],
                          allow_other=False, beam_size="garbage", vad_sensitivity="x")
    assert result["beam"] == 1
    assert result["vad_ms"] == 300
    assert t._model.calls[0]["vad_parameters"]["min_silence_duration_ms"] == 300


def test_invalid_vad_sensitivity_with_empty_audio():
    t = _loaded("small")
    result = t.transcribe(np.zeros(0, dtype=np.float32), vad_sensitivity="x")
    assert result["vad_ms"] == 300
