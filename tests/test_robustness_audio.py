import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

from unittest import mock  # noqa: E402

import pytest  # noqa: E402

import _robustness_stubs  # noqa: E402

_robustness_stubs.stub_sounddevice()
import audio_recorder  # noqa: E402


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
