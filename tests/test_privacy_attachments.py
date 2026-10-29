"""Saved attachments don't outlive their day; inline copied text can't break
out of its quotes in a terminal."""

import os
import sys
import tempfile
import time
import types

# Before config is imported (it creates its folder on import).
_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["XDG_CONFIG_HOME"] = _TMP
os.environ["APPDATA"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# text_output needs pyperclip/Windows APIs; compose() doesn't use it.
if "text_output" not in sys.modules:
    try:
        import text_output  # noqa: F401
    except ImportError:
        sys.modules["text_output"] = types.SimpleNamespace(
            insert_text=lambda *a, **k: True, paste_formats=lambda *a, **k: True)

import pytest  # noqa: E402

import context_capture as cc  # noqa: E402
from context_capture import Attachment, ContextCapture, Target, _Clip, compose  # noqa: E402

TERMINAL = Target("terminal", "WindowsTerminal.exe")


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


def test_old_attachments_removed_at_start(tmp_path):
    old = _old_file(tmp_path, "notype-20260101-000000-1.png", cc.KEEP_FILES_S + 60)
    fresh = _old_file(tmp_path, "notype-20260101-000000-2.txt", 60)
    other = _old_file(tmp_path, "keep-me.png", cc.KEEP_FILES_S + 60)
    ContextCapture(str(tmp_path), lambda info: None)
    assert _wait_for(lambda: not os.path.exists(old))
    assert os.path.exists(fresh) and os.path.exists(other)


def test_start_without_folder_is_fine(tmp_path):
    ContextCapture(str(tmp_path / "missing"), lambda info: None)._cleanup()


def test_cleanup_runs_on_a_timer(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CLEANUP_EVERY_S", 0.05)
    ContextCapture(str(tmp_path), lambda info: None)
    time.sleep(0.1)  # first sweep done, folder was empty
    old = _old_file(tmp_path, "notype-20260101-000000-1.png", cc.KEEP_FILES_S + 60)
    assert _wait_for(lambda: not os.path.exists(old))


def test_every_save_cleans_up(tmp_path, monkeypatch):
    capture = ContextCapture(str(tmp_path), lambda info: None)
    time.sleep(0.05)
    old = _old_file(tmp_path, "notype-20260101-000000-1.png", cc.KEEP_FILES_S + 60)
    capture._save(b"one", "txt")
    assert not os.path.exists(old)
    old = _old_file(tmp_path, "notype-20260101-000000-2.png", cc.KEEP_FILES_S + 60)
    capture._save(b"two", "txt")  # used to be skipped for an hour
    assert not os.path.exists(old)


@pytest.mark.parametrize("text", [
    'say "hi" to me',
    "costs $HOME",
    "run `rm -rf ~`",
    "echo $(whoami)",
    "%USERPROFILE%",
    "C:\\Temp\\",
])
def test_unsafe_text_goes_to_a_file(tmp_path, text):
    capture = ContextCapture(str(tmp_path), lambda info: None)
    item = capture._attachment(_Clip("text", text=text), 1, TERMINAL)
    with open(item.path, encoding="utf-8") as f:
        assert f.read() == text
    out = compose("look at this", [item], TERMINAL)
    assert text not in out and out.endswith("look at this")


def test_safe_text_still_goes_inline(tmp_path):
    capture = ContextCapture(str(tmp_path), lambda info: None)
    item = capture._attachment(_Clip("text", text="  error 42 in main.py  "), 1, TERMINAL)
    assert item.path == ""
    assert compose("fix it", [item], TERMINAL) == 'fix it "error 42 in main.py"'


def test_compose_never_breaks_the_quotes():
    # Built without _attachment (no file): left out, not inserted half-quoted.
    item = Attachment("text", 1, "text", text='a" ; calc "b')
    assert compose("fix it", [item], TERMINAL) == "fix it"


def test_chat_target_unchanged():
    chat = Target("chat", "chrome.exe")
    item = Attachment("text", 1, "text", text='say "hi"')
    assert compose("look", [item], chat) == 'look\n\n> say "hi"'
