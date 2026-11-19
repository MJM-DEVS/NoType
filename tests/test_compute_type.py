"""Compute types per device and the fallbacks of Transcriber.load_model().

WhisperModel is replaced by a fake that rejects what CTranslate2 rejects on
that device, so nothing is downloaded and no GPU library is needed."""

import sys
import types

import pytest

import faster_whisper
import faster_whisper.utils
from transcriber import DOWNLOAD_FAILED, Transcriber, compute_type_attempts, resolve_compute_type

CPU_REJECTS = ("float16", "int8_float16", "bfloat16", "int8_bfloat16")


def rejected(compute_type):
    return ValueError(f"Requested {compute_type} compute type, but the target device or "
                      f"backend do not support efficient {compute_type} computation.")


@pytest.fixture
def whisper(monkeypatch):
    """A fake WhisperModel: `fails` holds the (device, compute_type) pairs it
    rejects, `calls` what it was asked for. The model counts as downloaded."""
    state = types.SimpleNamespace(calls=[], fails=set())

    class FakeWhisperModel:
        def __init__(self, source, device, compute_type):
            state.calls.append((device, compute_type))
            if (device, compute_type) in state.fails:
                raise rejected(compute_type)
            self.source = source

    for compute_type in CPU_REJECTS:
        state.fails.add(("cpu", compute_type))
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)
    monkeypatch.setattr(Transcriber, "_local_model_path", lambda self: "/models/small")
    return state


def no_cuda(monkeypatch, count=0):
    monkeypatch.setitem(sys.modules, "ctranslate2",
                        types.SimpleNamespace(get_cuda_device_count=lambda: count))


# ── resolve_compute_type / compute_type_attempts ──

@pytest.mark.parametrize("device, compute_type, expected", [
    ("cuda", "auto", "float16"),
    ("cuda", None, "float16"),
    ("cuda", "", "float16"),
    ("cuda", "int8_float16", "int8_float16"),
    ("cuda", "int8", "int8"),
    ("cpu", "auto", "int8"),
    ("cpu", None, "int8"),
    ("cpu", "float16", "int8"),         # what the settings window used to save
    ("cpu", "int8_float16", "int8"),
    ("cpu", " Float16 ", "int8"),
    ("cpu", "int8", "int8"),
    ("cpu", "float32", "float32"),
])
def test_resolve_compute_type(device, compute_type, expected):
    assert resolve_compute_type(device, compute_type) == expected


@pytest.mark.parametrize("device, compute_type, expected", [
    ("cpu", "auto", ["int8", "float32"]),
    ("cpu", "float16", ["int8", "float32"]),
    ("cpu", "float32", ["float32", "int8"]),
    ("cuda", "auto", ["float16", "int8"]),
    ("cuda", "int8_float16", ["int8_float16", "float16", "int8"]),
    ("cuda", "int8", ["int8", "float16"]),
])
def test_compute_type_attempts(device, compute_type, expected):
    assert compute_type_attempts(device, compute_type) == expected


# ── Transcriber: device and compute type ──

def test_no_cuda_persisted_float16_becomes_int8(monkeypatch):
    no_cuda(monkeypatch)
    t = Transcriber("small", device="auto", compute_type="float16")
    assert (t.device, t.compute_type) == ("cpu", "int8")


def test_no_ctranslate2_counts_as_no_cuda(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2", None)  # import fails
    t = Transcriber("small", device="auto", compute_type="int8_float16")
    assert (t.device, t.compute_type) == ("cpu", "int8")


def test_cuda_keeps_the_configured_type(monkeypatch):
    no_cuda(monkeypatch, count=1)
    assert Transcriber("small", "auto", "auto").compute_type == "float16"
    assert Transcriber("small", "auto", "int8_float16").compute_type == "int8_float16"


def test_explicit_cpu_with_gpu_type():
    assert Transcriber("small", device="cpu", compute_type="float16").compute_type == "int8"
    assert Transcriber("small", device="cpu", compute_type="float32").compute_type == "float32"


# ── load_model ──

def test_persisted_float16_loads_on_cpu_without_error(monkeypatch, whisper):
    no_cuda(monkeypatch)
    t = Transcriber("small", device="auto", compute_type="float16")
    t.load_model()
    assert t.is_loaded
    assert t.compute_type == "int8"
    assert whisper.calls == [("cpu", "int8")]


def test_cpu_falls_back_to_float32(whisper):
    whisper.fails.add(("cpu", "int8"))
    status = []
    t = Transcriber("small", device="cpu", compute_type="auto")
    t.load_model(on_progress=status.append)
    assert t.compute_type == "float32"
    assert whisper.calls == [("cpu", "int8"), ("cpu", "float32")]
    assert any("float32" in s for s in status)
    assert status[-1] == "Modell geladen ✓"


def test_cpu_raises_with_device_and_types_once_all_failed(whisper):
    whisper.fails.update({("cpu", "int8"), ("cpu", "float32")})
    t = Transcriber("small", device="cpu", compute_type="float16")
    with pytest.raises(RuntimeError) as info:
        t.load_model()
    message = str(info.value)
    assert "cpu" in message and "int8, float32" in message
    assert not t.is_loaded


def test_cuda_int8_float16_falls_back_to_float16(whisper):
    whisper.fails.add(("cuda", "int8_float16"))
    t = Transcriber("small", device="cuda", compute_type="int8_float16")
    t.load_model()
    assert t.compute_type == "float16"
    assert whisper.calls == [("cuda", "int8_float16"), ("cuda", "float16")]


def test_cuda_tries_int8_before_giving_up(whisper):
    whisper.fails.update({("cuda", "int8_float16"), ("cuda", "float16")})
    t = Transcriber("small", device="cuda", compute_type="int8_float16")
    t.load_model()
    assert t.compute_type == "int8"


def test_cuda_all_failed(whisper):
    whisper.fails.update({("cuda", "float16"), ("cuda", "int8")})
    t = Transcriber("small", device="cuda", compute_type="auto")
    with pytest.raises(RuntimeError, match=r"on cuda, tried float16, int8"):
        t.load_model()


def test_download_happens_once_and_failure_is_named(monkeypatch, whisper):
    monkeypatch.setattr(Transcriber, "_local_model_path", lambda self: None)
    downloads = []

    def offline(repo):
        downloads.append(repo)
        raise OSError("Name or service not known")

    monkeypatch.setattr(faster_whisper.utils, "download_model", offline)
    t = Transcriber("small", device="cpu")
    with pytest.raises(RuntimeError) as info:
        t.load_model()
    assert str(info.value).startswith(DOWNLOAD_FAILED)
    assert downloads == ["small"]
    assert whisper.calls == []   # no load attempt without the files


def test_downloaded_once_then_loaded_from_the_folder(monkeypatch, whisper):
    monkeypatch.setattr(Transcriber, "_local_model_path", lambda self: None)
    monkeypatch.setattr(faster_whisper.utils, "download_model", lambda repo: f"/cache/{repo}")
    seen = []
    real = faster_whisper.WhisperModel

    def model(source, device, compute_type):
        seen.append(source)
        return real(source, device, compute_type)

    monkeypatch.setattr(faster_whisper, "WhisperModel", model)
    whisper.fails.add(("cpu", "int8"))
    Transcriber("small", device="cpu").load_model()
    assert seen == ["/cache/small", "/cache/small"]
