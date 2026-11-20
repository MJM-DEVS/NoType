"""Compute types per device and the fallbacks of Transcriber.load_model().

WhisperModel is replaced by a fake that rejects what CTranslate2 rejects on
that device, so nothing is downloaded and no GPU library is needed."""

import logging
import os
import sys
import time
import types

import pytest

import faster_whisper
import faster_whisper.utils
import transcriber
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


def test_a_failed_cuda_check_says_why_in_the_log(monkeypatch, caplog):
    def broken():
        raise OSError("DLL load failed while importing _ext")

    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(get_cuda_device_count=broken))
    with caplog.at_level(logging.INFO, logger="NoType.Transcriber"):
        t = Transcriber("small", device="auto", compute_type="auto")
    assert t.device == "cpu"
    assert "CUDA check failed, using the processor: OSError: DLL load failed" in caplog.text


def test_only_the_processor_converts_the_compute_type(monkeypatch, caplog):
    no_cuda(monkeypatch, count=1)
    with caplog.at_level(logging.INFO, logger="NoType.Transcriber"):
        assert Transcriber("small", "auto", "Float16").compute_type == "float16"
    assert "does not run on the processor" not in caplog.text
    no_cuda(monkeypatch)
    with caplog.at_level(logging.INFO, logger="NoType.Transcriber"):
        assert Transcriber("small", "auto", "float16").compute_type == "int8"
    assert "compute_type=float16 does not run on the processor, using int8" in caplog.text


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


# ── Download: a sign of life while the files grow ──

@pytest.fixture
def cache_blob(monkeypatch, tmp_path, whisper):
    """A Hugging Face cache in tmp_path, looked at every 50 ms; returns the
    blob that grows while the fake download runs."""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("HF_XET_CACHE", str(tmp_path / "xet"))
    monkeypatch.setattr(transcriber, "DOWNLOAD_BEAT", 0.05)
    monkeypatch.setattr(Transcriber, "_local_model_path", lambda self: None)
    return tmp_path / "hub" / "models--Systran--faster-whisper-small" / "blobs" / "abc.incomplete"


def test_a_growing_download_reports_progress(monkeypatch, cache_blob):
    def download(repo):
        cache_blob.parent.mkdir(parents=True)
        for _ in range(4):
            with open(cache_blob, "ab") as f:
                f.write(b"\0" * 2 ** 20)
            time.sleep(0.2)
        return "/cache/small"

    monkeypatch.setattr(faster_whisper.utils, "download_model", download)
    status = []
    Transcriber("small", device="cpu").load_model(on_progress=status.append)
    beats = [s for s in status if s.startswith("Lade Modell herunter")]
    assert len(beats) >= 2
    assert beats[-1] in ("Lade Modell herunter … 3 MB", "Lade Modell herunter … 4 MB")
    assert status[-1] == "Modell geladen ✓"


def test_a_stalled_download_stays_quiet(monkeypatch, cache_blob):
    def download(repo):
        time.sleep(0.4)   # nothing arrives
        return "/cache/small"

    monkeypatch.setattr(faster_whisper.utils, "download_model", download)
    status = []
    Transcriber("small", device="cpu").load_model(on_progress=status.append)
    assert not [s for s in status if s.startswith("Lade Modell herunter")]


def test_download_folders_follow_the_built_in_names(monkeypatch):
    monkeypatch.setenv("HF_HOME", "/hf")
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_XET_CACHE", raising=False)
    assert transcriber._download_folders("small") == [
        os.path.join("/hf", "hub", "models--Systran--faster-whisper-small"), os.path.join("/hf", "xet")]
    assert transcriber._download_folders("distil-whisper/distil-large-v3.5-ct2")[0].endswith(
        "models--distil-whisper--distil-large-v3.5-ct2")
