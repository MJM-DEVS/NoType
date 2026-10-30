"""Which clipboard formats NoType sets, and in which order.

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


def test_privacy_formats_come_first_then_content(text_output):
    fake = FakeWin32()
    text = ("hi\r\n" + "\0").encode("utf-16-le")
    assert place(text_output, fake, {text_output.CF_UNICODETEXT: text})
    assert [n for n, _ in fake.set] == PRIVACY + [text_output.CF_UNICODETEXT]
    assert fake.set[1][1] == b"\0\0\0\0"  # CanIncludeInClipboardHistory = 0
    assert fake.set[2][1] == b"\0\0\0\0"  # CanUploadToCloudClipboard = 0
    assert fake.set[3][1] == text


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
