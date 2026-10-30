"""Saved attachments don't outlive their day; inline copied text can't break
out of its quotes in a terminal."""

import importlib
import os
import sys
import tempfile
import threading
import time
import types

# Before config is imported (it creates its folder on import).
_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["XDG_CONFIG_HOME"] = _TMP
os.environ["APPDATA"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def cc():
    """context_capture, with a stand-in text_output where that can't load
    (it needs pyperclip/Windows APIs; compose() doesn't use it). The stand-in
    and the module built on it are gone again after this file."""
    try:
        importlib.import_module("text_output")
    except ImportError:
        pass
    else:
        yield importlib.import_module("context_capture")
        return
    had = sys.modules.pop("context_capture", None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "text_output", types.SimpleNamespace(
            insert_text=lambda *a, **k: True, paste_formats=lambda *a, **k: True))
        try:
            yield importlib.import_module("context_capture")
        finally:
            sys.modules.pop("context_capture", None)
            if had is not None:
                sys.modules["context_capture"] = had


@pytest.fixture
def capture(cc, tmp_path):
    """A ContextCapture on tmp_path whose cleanup timer ends with the test."""
    made = []

    def make(folder=None):
        made.append(cc.ContextCapture(str(folder or tmp_path), lambda info: None))
        return made[-1]
    yield make
    for c in made:
        c.stop_cleanup()


def _terminal(cc):
    return cc.Target("terminal", "WindowsTerminal.exe")


def _old_file(folder, name, age_s):
    path = os.path.join(folder, name)
    with open(path, "w") as f:
        f.write("x")
    past = time.time() - age_s
    os.utime(path, (past, past))
    return path


def _wait_for(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_old_attachments_removed_at_start(cc, capture, tmp_path):
    old = _old_file(tmp_path, "notype-20260101-000000-1.png", cc.KEEP_FILES_S + 60)
    fresh = _old_file(tmp_path, "notype-20260101-000000-2.txt", 60)
    other = _old_file(tmp_path, "keep-me.png", cc.KEEP_FILES_S + 60)
    capture()
    assert _wait_for(lambda: not os.path.exists(old))
    assert os.path.exists(fresh) and os.path.exists(other)


def test_start_without_folder_is_fine(capture, tmp_path):
    capture(tmp_path / "missing")._cleanup()


def test_cleanup_runs_on_a_timer(cc, capture, tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CLEANUP_EVERY_S", 0.05)
    capture()
    time.sleep(0.1)  # first sweep done, folder was empty
    old = _old_file(tmp_path, "notype-20260101-000000-1.png", cc.KEEP_FILES_S + 60)
    assert _wait_for(lambda: not os.path.exists(old))


def test_cleanup_timer_stops(cc, tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CLEANUP_EVERY_S", 0.05)
    before = set(threading.enumerate())
    c = cc.ContextCapture(str(tmp_path), lambda info: None)
    sweeper = [t for t in threading.enumerate()
               if t not in before and t.name == "attachment-cleanup"]
    assert sweeper
    c.stop_cleanup()
    for t in sweeper:
        t.join(1)
    assert not any(t.is_alive() for t in sweeper)


def test_every_save_cleans_up(cc, capture, tmp_path):
    c = capture()
    time.sleep(0.05)
    old = _old_file(tmp_path, "notype-20260101-000000-1.png", cc.KEEP_FILES_S + 60)
    c._save(b"one", "txt")
    assert not os.path.exists(old)
    old = _old_file(tmp_path, "notype-20260101-000000-2.png", cc.KEEP_FILES_S + 60)
    c._save(b"two", "txt")  # used to be skipped for an hour
    assert not os.path.exists(old)


@pytest.mark.parametrize("text", [
    'say "hi" to me',
    "say \u201chi\u201d to me",
    "sag \u201ehallo\u201c",
    "C:\\Temp\\",
    "x" * 161,
    "line one\nline two",
])
def test_unsafe_text_goes_to_a_file(cc, capture, text):
    terminal = _terminal(cc)
    item = capture()._attachment(cc._Clip("text", text=text), 1, terminal)
    with open(item.path, encoding="utf-8") as f:
        assert f.read() == text
    out = cc.compose("look at this", [item], terminal)
    assert text not in out and out.endswith("look at this")


@pytest.mark.parametrize("text", [
    "error 42 in main.py",
    "KeyError: 'id'",
    "100%",
    "echo $PATH",
    "%USERPROFILE%",
    "it failed!",
    "C:\\Temp\\file.txt",
])
def test_safe_text_still_goes_inline(cc, capture, text):
    terminal = _terminal(cc)
    item = capture()._attachment(cc._Clip("text", text=f"  {text}  "), 1, terminal)
    assert item.path == ""
    assert cc.compose("fix it", [item], terminal) == f'fix it "{text}"'


def test_chat_target_unchanged(cc):
    chat = cc.Target("chat", "chrome.exe")
    item = cc.Attachment("text", 1, "text", text='say "hi"')
    assert cc.compose("look", [item], chat) == 'look\n\n> say "hi"'
