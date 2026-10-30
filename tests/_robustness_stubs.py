"""Test helper: lets the backend modules import on a machine without
faster-whisper, sounddevice or Windows (CI on Linux). Only what is missing
is stubbed; an installed package is used as is.

The stubs sit in sys.modules only while the module under test imports, then
the previous entries come back. An entry another test put there (e.g. a
SimpleNamespace as text_output) is left alone if it has what we need, and
swapped out just for our import if it doesn't.

Test files set APPDATA/XDG_CONFIG_HOME to a temp dir BEFORE importing this,
because config.py creates its directory on import."""

import contextlib
import importlib
import os
import sys
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_MISSING = object()

_FASTER_WHISPER = {
    "faster_whisper": ("WhisperModel", "BatchedInferencePipeline"),
    "faster_whisper.utils": ("download_model",),
    "faster_whisper.vad": ("VadOptions",),
}
_SOUNDDEVICE = {"sounddevice": ("InputStream", "query_devices")}
# Windows-only modules backend.py imports (ctypes.windll, comtypes, hooks),
# with the names it takes from them.
_WINDOWS_ONLY = {
    "text_output": ("insert_text", "copy_selection"),
    "correction_watch": ("CorrectionWatcher",),
    "output_mute": ("OutputMuter",),
    "escape_hook": ("EscapeHook",),
    "context_capture": ("ContextCapture", "classify_foreground", "foreground_window",
                        "insert_with_context"),
}


def _usable(name: str, attrs) -> bool:
    """The module in sys.modules (or the real one, if it imports) has `attrs`."""
    mod = sys.modules.get(name)
    if mod is None:
        try:
            mod = importlib.import_module(name)
        except Exception:
            return False
    return all(hasattr(mod, a) for a in attrs)


def _is_repo_module(mod) -> bool:
    path = getattr(mod, "__file__", None)
    return bool(path) and os.path.dirname(os.path.abspath(path)) == ROOT


@contextlib.contextmanager
def _swapped(replacements: dict):
    """Put `replacements` into sys.modules, restore the old entries after."""
    saved = {name: sys.modules.get(name, _MISSING) for name in replacements}
    sys.modules.update(replacements)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def _faster_whisper_stubs() -> dict:
    if all(_usable(n, a) for n, a in _FASTER_WHISPER.items()):
        return {}
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
    return {"faster_whisper": fw, "faster_whisper.utils": utils,
            "faster_whisper.vad": vad}


def _mock_stubs(modules: dict) -> dict:
    return {name: mock.MagicMock(name=name)
            for name, attrs in modules.items() if not _usable(name, attrs)}


def _import_repo_module(name: str, replacements: dict):
    """Import the repo's `name` with `replacements` in sys.modules for the
    duration of the import. A foreign entry under `name` itself is put back
    afterwards; the caller keeps the real module."""
    if _is_repo_module(sys.modules.get(name)):
        return sys.modules[name]
    with _swapped(replacements):
        old = sys.modules.pop(name, _MISSING)
        try:
            return importlib.import_module(name)
        finally:
            if old is not _MISSING:
                sys.modules[name] = old


def import_transcriber():
    return _import_repo_module("transcriber", _faster_whisper_stubs())


def import_audio_recorder():
    return _import_repo_module("audio_recorder", _mock_stubs(_SOUNDDEVICE))


def import_backend():
    deps = {"transcriber": import_transcriber(),
            "audio_recorder": import_audio_recorder()}
    return _import_repo_module("backend", {
        **_faster_whisper_stubs(), **_mock_stubs(_SOUNDDEVICE),
        **_mock_stubs(_WINDOWS_ONLY), **deps})
