"""Test setup: runs before any NoType module is imported.

config.py creates its settings folder at import time, so APPDATA and
XDG_CONFIG_HOME point into a throwaway directory first. Modules that need
Windows-only or heavy packages (faster-whisper, pyperclip) get stand-ins
when those aren't installed; the tests only touch the pure functions.
"""

import atexit
import os
import shutil
import sys
import tempfile
import types

_CONFIG_ROOT = tempfile.mkdtemp(prefix="notype-tests-")
atexit.register(shutil.rmtree, _CONFIG_ROOT, ignore_errors=True)
os.environ["APPDATA"] = _CONFIG_ROOT
os.environ["XDG_CONFIG_HOME"] = _CONFIG_ROOT


def _stub(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Unavailable:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("faster-whisper is stubbed in the tests")


try:
    import faster_whisper  # noqa: F401
except ImportError:
    fw = _stub("faster_whisper", WhisperModel=_Unavailable, BatchedInferencePipeline=_Unavailable)
    fw.utils = _stub("faster_whisper.utils", download_model=_Unavailable)
    fw.vad = _stub("faster_whisper.vad", VadOptions=_Unavailable)

try:
    import pyperclip  # noqa: F401
except ImportError:
    _stub("pyperclip", copy=lambda text: None, paste=lambda: "")
