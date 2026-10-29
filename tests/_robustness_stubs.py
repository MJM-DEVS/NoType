"""Test helper: lets the backend modules import on a machine without
faster-whisper, sounddevice or Windows (CI on Linux). Only what fails to
import is stubbed; an installed package is used as is.

Test files set APPDATA/XDG_CONFIG_HOME to a temp dir BEFORE importing this,
because config.py creates its directory on import."""

import importlib
import os
import sys
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Windows-only modules backend.py imports (ctypes.windll, comtypes, hooks).
_WINDOWS_ONLY = ["text_output", "correction_watch", "output_mute",
                 "escape_hook", "context_capture"]


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def stub_faster_whisper() -> None:
    """faster_whisper, if missing, as a stub. Tests replace WhisperModel."""
    if _importable("faster_whisper"):
        return
    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = mock.MagicMock(name="WhisperModel")
    fw.BatchedInferencePipeline = mock.MagicMock(name="BatchedInferencePipeline")
    utils = types.ModuleType("faster_whisper.utils")

    def download_model(*args, **kwargs):
        raise OSError("faster_whisper stub: nothing downloaded")

    utils.download_model = download_model
    vad = types.ModuleType("faster_whisper.vad")
    vad.VadOptions = dict
    fw.utils, fw.vad = utils, vad
    sys.modules.update({"faster_whisper": fw, "faster_whisper.utils": utils,
                        "faster_whisper.vad": vad})


def stub_sounddevice() -> None:
    if not _importable("sounddevice"):
        sys.modules["sounddevice"] = mock.MagicMock(name="sounddevice")


def import_backend():
    """backend.py with the Windows-only modules stubbed where they don't
    import. The stubs leave sys.modules again afterwards, so other tests
    still see the real modules (or their real import error)."""
    stub_faster_whisper()
    stub_sounddevice()
    if "backend" in sys.modules:
        return sys.modules["backend"]
    stubbed = []
    for name in _WINDOWS_ONLY:
        if name not in sys.modules and not _importable(name):
            sys.modules[name] = mock.MagicMock(name=name)
            stubbed.append(name)
    try:
        return importlib.import_module("backend")
    finally:
        for name in stubbed:
            sys.modules.pop(name, None)
