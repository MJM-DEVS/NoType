import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

import threading  # noqa: E402
from unittest import mock  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import _robustness_stubs  # noqa: E402

backend = _robustness_stubs.import_backend()


@pytest.fixture
def b(monkeypatch):
    """A Backend with every Windows/IO side mocked and sent messages recorded."""
    monkeypatch.setattr(backend, "save_config", mock.Mock())
    monkeypatch.setattr(backend, "insert_text", mock.Mock(return_value=True))
    inst = backend.Backend()
    inst.config = dict(backend.load_config())
    inst.config.update(cleanup_mode="fast", spoken_punctuation=False, learn_corrections=True)
    inst.sent = []
    inst.send = lambda t, d=None: inst.sent.append((t, d or {}))
    inst.context = mock.MagicMock()
    inst.corrections = mock.MagicMock()
    inst.esc_hook = mock.MagicMock()
    inst.muter = mock.MagicMock()
    return inst


def _stop_with(b, seconds, text="hallo welt", **result):
    b.recorder = mock.Mock()
    b.recorder.stop.return_value = np.zeros(int(seconds * backend.SAMPLE_RATE), dtype=np.float32)
    b.transcriber = mock.Mock(is_loaded=True)
    b.transcriber.transcribe.return_value = {
        "text": text, "beam": 1, "compute_type": "float16", "duration": seconds,
        "language": "de", **result}
    b.handle_stop_recording()
    return [d for t, d in b.sent if t == "transcription_done"]


def test_min_length_counts_samples_not_wall_clock(b):
    # Held for long (wall clock), but only 0.2 s of audio arrived.
    b._record_start_time = 0
    done = _stop_with(b, 0.2)
    assert done == [{"text": "", "empty": True, "reason": "too_short"}]
    b.transcriber.transcribe.assert_not_called()


def test_enough_audio_is_transcribed(b):
    done = _stop_with(b, 1.0)
    assert done and done[0]["text"] == "hallo welt"


def test_failed_save_keeps_transcribed_text(b):
    backend.save_config.side_effect = OSError("disk full")
    done = _stop_with(b, 1.0, compute_fallback=True)
    assert done and done[0]["text"] == "hallo welt"
    backend.insert_text.assert_called_once()
    assert b.config["compute_type"] == "float16"


def test_broken_dictionary_entries_do_not_fail_dictation(b):
    b.config["dictionary"] = ["oops", {"from": "welt", "to": "Welt"}, 42]
    done = _stop_with(b, 1.0)
    assert done and done[0]["text"] == "hallo Welt"
    assert not [d for t, d in b.sent if t == "error"]


def test_dictionary_not_a_list(b):
    b.config["dictionary"] = {"from": "welt", "to": "Welt"}
    done = _stop_with(b, 1.0)
    assert done and done[0]["text"] == "hallo welt"


def test_handle_save_config_survives_save_error(b, monkeypatch):
    backend.save_config.side_effect = OSError("locked")
    monkeypatch.setattr(b, "init_transcriber", mock.Mock())
    b.handle_save_config({"hotkey": "f9"})
    assert b.config["hotkey"] == "f9"
    assert ("error", {"message": "Saving settings failed"}) in b.sent
    assert [t for t, _ in b.sent][-1] == "config_saved"


def test_init_transcriber_runs_one_load_and_skips_superseded(b, monkeypatch):
    """Three model changes while the first load runs: loads never overlap,
    the middle request is skipped, the newest one loads last."""
    started, release = threading.Event(), threading.Event()
    active, max_active, loaded = [0], [0], []
    lock = threading.Lock()

    def fake_load():
        with lock:
            active[0] += 1
            max_active[0] = max(max_active[0], active[0])
        size = b.config["model_size"]
        if size == "small":
            started.set()
            release.wait(5)
        loaded.append(size)
        with lock:
            active[0] -= 1

    monkeypatch.setattr(b, "_load_transcriber", fake_load)
    b.config["model_size"] = "small"
    first = threading.Thread(target=b.init_transcriber)
    first.start()
    assert started.wait(5)
    waiting = []
    for size in ("medium", "large-v3"):
        b.config["model_size"] = size
        t = threading.Thread(target=b.init_transcriber)
        t.start()
        waiting.append(t)
    # both requests have taken their generation number before the release
    for _ in range(500):
        if b._model_generation == 3:
            break
        threading.Event().wait(0.01)
    release.set()
    for t in [first, *waiting]:
        t.join(5)
    assert max_active[0] == 1
    assert loaded == ["small", "large-v3"]
