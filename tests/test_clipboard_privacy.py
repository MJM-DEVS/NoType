"""Which clipboard formats NoType sets when it pastes.

text_output needs ctypes.windll and pyperclip at import time; both are
stubbed here and restored after each test. Only the format logic is tested,
not the real Win32 calls."""

import ctypes
import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRIVACY = ["ExcludeClipboardContentFromMonitorProcessing",
           "CanIncludeInClipboardHistory",
           "CanUploadToCloudClipboard"]


@pytest.fixture
def text_output(monkeypatch):
    monkeypatch.setattr(ctypes, "windll", mock.MagicMock(), raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", mock.MagicMock(), raising=False)
    monkeypatch.setitem(sys.modules, "pyperclip", types.ModuleType("pyperclip"))
    monkeypatch.delitem(sys.modules, "text_output", raising=False)
    import text_output as mod
    yield mod
    sys.modules.pop("text_output", None)


class FakeWin32:
    """Records SetClipboardData calls as (format name or id, bytes)."""

    def __init__(self, fail_register=False, fail_set=()):
        self.ids = {}
        self.mem = {}
        self.set = []
        self.freed = []
        self.fail_register = fail_register
        self.fail_set = set(fail_set)

    def RegisterClipboardFormatW(self, name):
        if self.fail_register:
            raise OSError("no")
        return self.ids.setdefault(name, 0xC000 + len(self.ids))

    def GlobalAlloc(self, flags, size):
        h = len(self.mem) + 1
        self.mem[h] = bytearray(size)
        return h

    def GlobalLock(self, h):
        return h

    def GlobalUnlock(self, h):
        pass

    def GlobalFree(self, h):
        self.freed.append(h)

    def memmove(self, ptr, data, size):
        self.mem[ptr][:size] = data[:size]

    def SetClipboardData(self, fmt, h):
        if fmt in self.fail_set:
            return 0
        names = {v: k for k, v in self.ids.items()}
        self.set.append((names.get(fmt, fmt), bytes(self.mem[h])))
        return h


def place(mod, fake, formats):
    return mod._place_formats(formats, u32=fake, k32=fake, memmove=fake.memmove)


def test_all_markers_and_the_content_are_set(text_output):
    fake = FakeWin32()
    text = ("hi\r\n" + "\0").encode("utf-16-le")
    assert place(text_output, fake, {text_output.CF_UNICODETEXT: text})
    placed = dict(fake.set)
    assert set(placed) == set(PRIVACY) | {text_output.CF_UNICODETEXT}
    for name in PRIVACY:
        assert placed[name] == b"\0\0\0\0"  # DWORD 0
    assert placed[text_output.CF_UNICODETEXT] == text


def test_image_formats_get_the_markers_too(text_output):
    fake = FakeWin32()
    assert place(text_output, fake, {8: b"DIB", 0xC123: b"PNG"})
    assert [n for n, _ in fake.set] == PRIVACY + [8, 0xC123]


def test_failing_markers_never_block_the_paste(text_output):
    fake = FakeWin32(fail_register=True)
    assert place(text_output, fake, {13: b"x\0"})
    assert fake.set == [(13, b"x\0")]


def test_rejected_marker_is_freed_and_paste_continues(text_output):
    fake = FakeWin32()
    hist = fake.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
    fake.fail_set = {hist}
    assert place(text_output, fake, {13: b"x\0"})
    names = [n for n, _ in fake.set]
    assert "CanIncludeInClipboardHistory" not in names
    assert names[-1] == 13
    assert len(fake.freed) == 1


def test_no_content_placed_reports_failure(text_output):
    fake = FakeWin32(fail_set={13})
    assert not place(text_output, fake, {13: b"x\0"})


def _quiet_paste(mod, monkeypatch):
    # user32 is None off Windows once the import guards land; stub it either way.
    monkeypatch.setattr(mod, "user32", mock.MagicMock(), raising=False)
    monkeypatch.setattr(mod, "_wait_modifiers_released", lambda *a: None)
    monkeypatch.setattr(mod, "_wait_clipboard_updated", lambda *a: True)
    monkeypatch.setattr(mod, "_send_ctrl_v", lambda: True)


def test_insert_text_puts_crlf_utf16_text_on_the_clipboard(text_output, monkeypatch):
    _quiet_paste(text_output, monkeypatch)
    calls = []
    monkeypatch.setattr(text_output, "_set_clipboard", lambda f: calls.append(f) or True)
    assert text_output.insert_text("a\nb")
    assert calls == [{13: "a\r\nb\0".encode("utf-16-le")}]


def test_insert_text_types_when_the_clipboard_cannot_be_set(text_output, monkeypatch):
    _quiet_paste(text_output, monkeypatch)
    monkeypatch.setattr(text_output, "_set_clipboard", lambda f: False)
    typed = []
    result = object()
    monkeypatch.setattr(text_output, "_type_unicode", lambda t: typed.append(t) or result)
    assert text_output.insert_text("hello") is result
    assert typed == ["hello"]
