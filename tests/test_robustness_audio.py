import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

from unittest import mock  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import _robustness_stubs  # noqa: E402

audio_recorder = _robustness_stubs.import_audio_recorder()


def test_start_failure_leaves_recorder_idle(monkeypatch):
    sd = mock.Mock()
    sd.InputStream.side_effect = OSError("no input device")
    monkeypatch.setattr(audio_recorder, "sd", sd)
    rec = audio_recorder.AudioRecorder()
    with pytest.raises(OSError):
        rec.start()
    assert not rec.is_recording


def test_stream_start_failure_closes_stream(monkeypatch):
    stream = mock.Mock()
    stream.start.side_effect = RuntimeError("device busy")
    sd = mock.Mock()
    sd.InputStream.return_value = stream
    monkeypatch.setattr(audio_recorder, "sd", sd)
    rec = audio_recorder.AudioRecorder()
    with pytest.raises(RuntimeError):
        rec.start()
    assert not rec.is_recording
    stream.close.assert_called_once()
    assert len(rec.stop()) == 0


def test_start_success_records(monkeypatch):
    sd = mock.Mock()
    monkeypatch.setattr(audio_recorder, "sd", sd)
    rec = audio_recorder.AudioRecorder()
    rec.start()
    assert rec.is_recording
    sd.InputStream.return_value.start.assert_called_once()
    rec.stop()
    sd.InputStream.return_value.close.assert_called_once()


def _started(monkeypatch, **kwargs):
    """A recorder on a fake stream; returns it and the fake sounddevice."""
    sd = mock.Mock()
    monkeypatch.setattr(audio_recorder, "sd", sd)
    rec = audio_recorder.AudioRecorder(**kwargs)
    rec.start()
    return rec, sd


def _feed(sd, *sizes):
    """Push blocks of `sizes` samples (block i filled with i) through the callback."""
    callback = sd.InputStream.call_args.kwargs["callback"]
    for i, n in enumerate(sizes):
        callback(np.full((n, 1), i, dtype=np.float32), n, None, None)


def test_take_is_capped_and_keeps_the_start(monkeypatch):
    monkeypatch.setattr(audio_recorder, "MAX_RECORD_SAMPLES", 1000)
    on_limit = mock.Mock()
    rec, sd = _started(monkeypatch, on_limit=on_limit)
    _feed(sd, 300, 300, 300, 300, 300)  # the cap falls inside block 3
    on_limit.assert_called_once()
    assert rec.is_recording
    audio = rec.stop()
    assert len(audio) == 1000
    assert audio[0] == 0 and audio[-1] == 3  # the start is kept, the rest dropped


def test_new_take_resets_the_cap(monkeypatch):
    monkeypatch.setattr(audio_recorder, "MAX_RECORD_SAMPLES", 500)
    on_limit = mock.Mock()
    rec, sd = _started(monkeypatch, on_limit=on_limit)
    _feed(sd, 600)
    assert len(rec.stop()) == 500
    rec.start()
    _feed(sd, 300)
    assert len(rec.stop()) == 300
    on_limit.assert_called_once()


def test_failing_limit_callback_does_not_break_recording(monkeypatch):
    monkeypatch.setattr(audio_recorder, "MAX_RECORD_SAMPLES", 100)
    rec, sd = _started(monkeypatch, on_limit=mock.Mock(side_effect=RuntimeError))
    _feed(sd, 300, 300)
    assert len(rec.stop()) == 100


def test_no_levels_after_the_cap(monkeypatch):
    monkeypatch.setattr(audio_recorder, "MAX_RECORD_SAMPLES", 500)
    on_amplitude = mock.Mock()
    rec, sd = _started(monkeypatch, on_amplitude=on_amplitude)
    _feed(sd, 300, 300, 300, 300)  # the cap falls inside block 1
    # Block 0 live, block 1 a flat update, then nothing.
    assert on_amplitude.call_count == 2
    assert on_amplitude.call_args.args == (0.0, [0.0] * audio_recorder.N_BANDS)
    assert rec.is_recording
