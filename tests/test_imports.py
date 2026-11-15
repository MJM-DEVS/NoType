"""Every module imports on Linux without the Windows-only packages, given
the stand-ins from conftest.py (faster-whisper, pyperclip)."""

import importlib
import os
import tempfile

import pytest


@pytest.mark.parametrize("name", [
    "agent_webhook", "context_capture", "correction_watch", "dictionary", "escape_hook",
    "hallucinations", "output_mute", "postprocess", "spoken_punctuation", "text_output",
    "transcriber",
])
def test_module_imports_off_windows(name):
    importlib.import_module(name)


def test_config_dir_is_a_temp_dir():
    config = importlib.import_module("config")
    tmp = os.path.realpath(tempfile.gettempdir())
    assert os.path.realpath(config.CONFIG_DIR).startswith(tmp)
