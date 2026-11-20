"""The backend on Windows: a start that fails says why in notype.log, and a
settings.json that can't be written costs neither the model nor the text."""

import contextlib
import os
import subprocess
import sys
import time
import types

import pytest

try:
    import sounddevice  # noqa: F401
except (ImportError, OSError):  # not installed, or no PortAudio
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")

import backend

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Python hands an exception nobody caught to sys.excepthook; the windowed
# PyInstaller build (console=False) shows it in a modal dialog instead, and
# the process only ends once someone clicks it away.
UNHANDLED_MARKER = "sys.excepthook = lambda *exc: sys.stderr.write('UNHANDLED\\n')\n"


def test_a_failed_import_lands_in_the_log(tmp_path):
    """Before: the traceback went to stderr, which only Electron's console
    sees; notype.log said nothing about a backend that never started."""
    home = os.path.dirname(REPO)   # the traceback's paths are below it
    code = ("import runpy, sys\n"
            "sys.modules['sounddevice'] = None   # like a PortAudio DLL that is missing\n"
            f"{UNHANDLED_MARKER}"
            f"runpy.run_path({os.path.join(REPO, 'backend.py')!r}, run_name='__main__')\n")
    env = {**os.environ, "APPDATA": str(tmp_path), "HOME": home, "USERPROFILE": home}
    done = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(tmp_path),
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    # A plain exit, no unhandled exception (see UNHANDLED_MARKER).
    assert done.returncode == 1
    assert b"UNHANDLED" not in done.stderr
    assert b"ModuleNotFoundError: import of sounddevice halted" in done.stderr
    assert b"Traceback" in done.stderr and done.stderr.count(b"Traceback") == 1
    log = (tmp_path / "NoType" / "notype.log").read_text(encoding="utf-8")
    assert "CRITICAL: Backend failed to start:" in log
    assert "ModuleNotFoundError: import of sounddevice halted" in log
    assert home not in log and "~" in log   # no user name from the install path


def test_a_failing_speech_engine_process_exits_without_a_dialog(tmp_path):
    """backend.exe --transcribe-worker: an exception that leaves main() goes
    to stderr (the backend logs it) and ends the process with 1."""
    code = ("import runpy, sys\n"
            f"sys.path.insert(0, {REPO!r})\n"
            "import transcribe_worker\n"
            "def main():\n"
            "    raise ZeroDivisionError('in the speech engine')\n"
            "transcribe_worker.main = main\n"
            f"{UNHANDLED_MARKER}"
            "sys.argv = ['backend.py', '--transcribe-worker']\n"
            f"runpy.run_path({os.path.join(REPO, 'backend.py')!r}, run_name='__main__')\n")
    done = subprocess.run([sys.executable, "-c", code], env={**os.environ, "APPDATA": str(tmp_path)},
                          cwd=str(tmp_path), stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=60)
    assert done.returncode == 1
    assert b"ZeroDivisionError: in the speech engine" in done.stderr
    assert b"UNHANDLED" not in done.stderr


class Sent(list):
    def __call__(self, kind, data=None):
        self.append((kind, data))

    def kinds(self):
        return [kind for kind, _ in self]


@pytest.fixture
def bare(monkeypatch):
    """A Backend without its hardware: just config, send() and what the
    paths below touch. save_config fails like a settings.json a virus
    scanner holds open."""
    b = backend.Backend.__new__(backend.Backend)
    b.config = {"cleanup_mode": "off", "learn_corrections": False, "spoken_punctuation": False,
                "ai_context": False, "dictionary": []}
    b.send = Sent()
    b._model_failed = True
    b._cancel_requested = False
    b.esc_hook = types.SimpleNamespace(disarm=lambda: None)
    b.context = types.SimpleNamespace(own_writes=contextlib.nullcontext)
    saves = []

    def locked(config):
        saves.append(dict(config))
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(backend, "save_config", locked)
    monkeypatch.setattr(backend.Backend, "_gpu_info", classmethod(lambda cls: None))
    b.saves = saves
    return b


def test_model_ready_although_the_fallback_cannot_be_saved(bare):
    bare._on_engine_ready({"device": "CUDA (float16)", "model": "small",
                           "compute_type": "float16", "compute_fallback": True})
    assert bare.send.kinds() == ["model_ready"]
    assert bare.config["compute_type"] == "float16" and len(bare.saves) == 1
    assert bare._model_failed is False


def test_the_dictation_is_inserted_although_the_fallback_cannot_be_saved(monkeypatch, bare):
    """Before: save_config() raised in the middle of _stop_and_transcribe, the
    finished text was never inserted and the dictation counted as failed."""
    inserted = []
    monkeypatch.setattr(backend, "insert_text", lambda text, method: inserted.append(text) or True)
    bare.engine = types.SimpleNamespace(transcribe=lambda audio, opts, **kw: {
        "text": "Hallo Welt", "beam": 1, "compute_type": "float16", "duration": 1.0,
        "compute_fallback": True, "language": "de"})
    take = {"audio": [0.0] * 16000, "context": None, "command": False, "agent": False,
            "version": "", "seconds": 1.0, "window": backend._foreground_hwnd(),
            "stopped_at": time.monotonic()}
    bare._stop_and_transcribe(take, {})
    assert inserted == ["Hallo Welt"]
    assert ("transcription_done", {"text": "Hallo Welt", "beam": 1, "compute_type": "float16",
                                   "duration": 1.0, "cleanup": "off", "cleanup_ms": 0}) in bare.send
    assert bare.config["compute_type"] == "float16" and len(bare.saves) == 1
