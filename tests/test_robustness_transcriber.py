import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

import gc  # noqa: E402
import threading  # noqa: E402
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


class CublasModel(FakeModel):
    """int8_float16 loads but fails on the first matmul; the float16 load
    waits for `release`, so a second pass can arrive mid-reload."""

    loads = []
    reloading = None
    release = None

    def __init__(self, source, device=None, compute_type=None):
        super().__init__(source, device, compute_type)
        self.compute_type = compute_type
        CublasModel.loads.append(compute_type)
        if compute_type == "float16":
            CublasModel.reloading.set()
            assert CublasModel.release.wait(5)

    def transcribe(self, audio, **kwargs):
        if self.compute_type == "int8_float16":
            raise RuntimeError("CUBLAS_STATUS_NOT_SUPPORTED")
        return super().transcribe(audio, **kwargs)


@pytest.fixture
def cublas(monkeypatch):
    monkeypatch.setattr(transcriber, "WhisperModel", CublasModel)
    CublasModel.loads = []
    CublasModel.reloading = threading.Event()
    CublasModel.release = threading.Event()
    t = Transcriber(model_size="small", device="cuda", compute_type="int8_float16")
    t.load_model()
    return t


def test_preview_waits_for_cublas_reload(cublas):
    audio = np.zeros(16000, dtype=np.float32)
    results, errors = {}, []

    def run(name, **kwargs):
        try:
            results[name] = cublas.transcribe(audio, **kwargs)
        except Exception as e:
            errors.append(e)

    final = threading.Thread(target=run, args=("final",))
    final.start()
    assert CublasModel.reloading.wait(5)
    # Mid-reload the model counts as loaded, so a preview pass starts and
    # waits for the lock instead of failing with "Model not loaded".
    assert cublas.is_loaded
    preview = threading.Thread(target=run, args=("preview",), kwargs={"beam_size": "1"})
    preview.start()
    preview.join(0.2)
    assert preview.is_alive()  # blocked on the inference lock
    CublasModel.release.set()
    final.join(5)
    preview.join(5)
    assert errors == []
    assert results["final"]["compute_fallback"] is True
    assert results["preview"]["compute_type"] == "float16"
    assert CublasModel.loads == ["int8_float16", "float16"]


def test_failed_cublas_reload_reports_not_loaded(cublas, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("out of VRAM")

    CublasModel.release.set()
    monkeypatch.setattr(transcriber, "WhisperModel", broken)
    with pytest.raises(OSError):
        cublas.transcribe(np.zeros(16000, dtype=np.float32))
    assert not cublas.is_loaded
    with pytest.raises(RuntimeError, match="Model not loaded"):
        cublas.transcribe(np.zeros(16000, dtype=np.float32))


def test_fallback_flag_is_set_by_the_reloading_pass(cublas):
    CublasModel.release.set()
    assert cublas.fell_back_to is None
    result = cublas.transcribe(np.zeros(16000, dtype=np.float32), beam_size="1")
    assert result["compute_fallback"] is True
    # Stays set for the backend even if nobody reads this (preview) result.
    assert cublas.fell_back_to == "float16"
    cublas.fell_back_to = None
    cublas.transcribe(np.zeros(16000, dtype=np.float32))
    assert cublas.fell_back_to is None


def test_two_passes_hitting_cublas_reload_once(cublas, monkeypatch):
    CublasModel.release.set()
    barrier = threading.Barrier(2)

    def failing(self, audio, **kwargs):
        # Both passes are inside the int8 model at once and both see the
        # error. The lock is dropped for the meeting only, or the second
        # pass could never get in.
        Transcriber.inference_lock.release()
        try:
            barrier.wait(5)
        finally:
            Transcriber.inference_lock.acquire()
        raise RuntimeError("CUBLAS_STATUS_NOT_SUPPORTED")

    original = CublasModel.transcribe
    monkeypatch.setattr(CublasModel, "transcribe", lambda self, audio, **kw: (
        failing(self, audio, **kw) if self.compute_type == "int8_float16"
        else original(self, audio, **kw)))
    results, errors = [], []

    def run():
        try:
            results.append(cublas.transcribe(np.zeros(16000, dtype=np.float32)))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert errors == []
    assert [r["compute_type"] for r in results] == ["float16", "float16"]
    assert CublasModel.loads.count("float16") == 1
    assert cublas.fell_back_to == "float16"
