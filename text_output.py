"""
NoType – Text Output
Inserts transcribed text into the currently active input field
using clipboard + Windows SendInput API for reliable Ctrl+V.
"""

import time
import logging
import ctypes
import ctypes.wintypes
import sys
import pyperclip

logger = logging.getLogger("NoType.TextOutput")

_WIN = sys.platform.startswith("win")

# ── Windows API ──
user32 = ctypes.windll.user32 if _WIN else None

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12  # Alt
VK_RETURN = 0x0D
VK_C = 0x43
VK_V = 0x56


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]


class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    _anonymous_ = ("_input",)
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("_input", _INPUT),
    ]


def _wait_modifiers_released(timeout: float = 0.3) -> bool:
    """Wait until Ctrl/Shift/Alt are physically released.

    The user's hotkey (e.g. ctrl+x) may still be held when transcription
    finishes; a physically-held Shift would turn our Ctrl+V into Ctrl+Shift+V
    (different action in many apps). Event-based: returns immediately when the
    keys are already up – replaces the old fixed 150 ms sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in (VK_CONTROL, VK_SHIFT, VK_MENU)):
            return True
        time.sleep(0.01)
    return False


def _wait_clipboard_updated(seq_before: int, timeout: float = 0.15) -> bool:
    """Wait until Windows' clipboard sequence number changes after our copy.
    Typically done in <15 ms – replaces the old fixed 100 ms sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user32.GetClipboardSequenceNumber() != seq_before:
            return True
        time.sleep(0.005)
    return False


def _type_unicode(text: str) -> bool:
    """Type `text` directly via SendInput KEYEVENTF_UNICODE key events.

    Works in targets where Ctrl+V doesn't (classic consoles, some elevated
    windows) and never touches the clipboard. Newlines are sent as real Enter
    presses; astral chars (emoji) go as UTF-16 surrogate pairs."""
    events = []
    for ch in text.replace("\r\n", "\n"):
        if ch in ("\n", "\r"):
            events.append((VK_RETURN, 0, 0))
            events.append((VK_RETURN, 0, KEYEVENTF_KEYUP))
            continue
        utf16 = ch.encode("utf-16-le")
        for code_unit in [int.from_bytes(utf16[i:i + 2], "little")
                          for i in range(0, len(utf16), 2)]:
            events.append((0, code_unit, KEYEVENTF_UNICODE))
            events.append((0, code_unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))

    # Send in batches – one SendInput call per 256 events keeps the payload
    # small enough for every target while still being effectively instant.
    for start in range(0, len(events), 256):
        batch = events[start:start + 256]
        inputs = (INPUT * len(batch))()
        for i, (vk, scan, flags) in enumerate(batch):
            inputs[i].type = INPUT_KEYBOARD
            inputs[i].ki.wVk = vk
            inputs[i].ki.wScan = scan
            inputs[i].ki.dwFlags = flags
        sent = user32.SendInput(len(batch), ctypes.byref(inputs), ctypes.sizeof(INPUT))
        if sent != len(batch):
            logger.warning(f"SendInput(unicode) sent {sent}/{len(batch)}")
            return False
    return True


def _send_ctrl(vk: int) -> bool:
    """Simulate Ctrl+<vk> using Windows SendInput API."""
    inputs = (INPUT * 4)()
    for i, (key, flags) in enumerate(((VK_CONTROL, 0), (vk, 0),
                                      (vk, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP))):
        inputs[i].type = INPUT_KEYBOARD
        inputs[i].ki.wVk = key
        inputs[i].ki.dwFlags = flags

    result = user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    if result != 4:
        logger.warning(f"SendInput returned {result}, expected 4 (error: {ctypes.GetLastError()})")
    return result == 4


def _send_ctrl_v():
    return _send_ctrl(VK_V)


# Separate DLL handles: pyperclip sets its own argtypes on the shared
# ctypes.windll function objects.
if _WIN:
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32.RegisterClipboardFormatW.argtypes = [ctypes.wintypes.LPCWSTR]
    _u32.RegisterClipboardFormatW.restype = ctypes.wintypes.UINT
    _u32.IsClipboardFormatAvailable.argtypes = [ctypes.wintypes.UINT]
    _u32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    _u32.GetClipboardData.argtypes = [ctypes.wintypes.UINT]
    _u32.GetClipboardData.restype = ctypes.wintypes.HANDLE
    _k32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    _k32.GlobalLock.restype = ctypes.c_void_p
    _k32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    _k32.GlobalSize.argtypes = [ctypes.wintypes.HGLOBAL]
    _k32.GlobalSize.restype = ctypes.c_size_t
    _u32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.HANDLE]
    _u32.SetClipboardData.restype = ctypes.wintypes.HANDLE
    _k32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
    _k32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
    _k32.GlobalFree.argtypes = [ctypes.wintypes.HGLOBAL]
    _k32.GlobalFree.restype = ctypes.wintypes.HGLOBAL
    _u32.CreateWindowExW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR,
                                     ctypes.wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.wintypes.HWND, ctypes.wintypes.HMENU, ctypes.wintypes.HINSTANCE,
                                     ctypes.wintypes.LPVOID]
    _u32.CreateWindowExW.restype = ctypes.wintypes.HWND
    _u32.DestroyWindow.argtypes = [ctypes.wintypes.HWND]
GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13

# Documented markers that keep what NoType puts on the clipboard out of
# clipboard history (Win+V), cloud clipboard sync and clipboard managers
# that honour it.
_PRIVACY_FORMATS = (
    ("ExcludeClipboardContentFromMonitorProcessing", b"\0\0\0\0"),  # any content
    ("CanIncludeInClipboardHistory", b"\0\0\0\0"),  # DWORD 0
    ("CanUploadToCloudClipboard", b"\0\0\0\0"),  # DWORD 0
)


def _clipboard_raw(format_name: str) -> bytes | None:
    """Raw bytes of a registered clipboard format, None if absent."""
    fmt = _u32.RegisterClipboardFormatW(format_name)
    if not fmt or not _u32.IsClipboardFormatAvailable(fmt):
        return None
    for _ in range(5):  # another app may hold the clipboard for a moment
        if _u32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return None
    try:
        h = _u32.GetClipboardData(fmt)
        ptr = _k32.GlobalLock(h) if h else None
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr, _k32.GlobalSize(h))
        finally:
            _k32.GlobalUnlock(h)
    finally:
        _u32.CloseClipboard()


def _place_one(fmt: int, data: bytes, u32, k32, memmove) -> bool:
    """Put one format on the already opened and emptied clipboard."""
    h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    ptr = k32.GlobalLock(h) if h else None
    if not ptr:
        if h:
            k32.GlobalFree(h)
        return False
    memmove(ptr, data, len(data))
    k32.GlobalUnlock(h)
    if u32.SetClipboardData(fmt, h):
        return True  # the clipboard owns the memory now
    k32.GlobalFree(h)
    return False


def _place_formats(formats: dict, u32=None, k32=None, memmove=None) -> bool:
    """Put `formats` plus the privacy markers on the opened, emptied clipboard.

    Used for everything NoType pastes, including copies of the user's own
    images and texts it pastes along into AI apps (context_capture). Their
    original copy is already in the history (if it is on), so nothing of
    theirs is lost.
    The Win32 handles are injectable for tests."""
    u32 = u32 or _u32
    k32 = k32 or _k32
    memmove = memmove or ctypes.memmove
    # Markers are best effort: a failure is only logged.
    for name, data in _PRIVACY_FORMATS:
        try:
            fmt = u32.RegisterClipboardFormatW(name)
            if not fmt or not _place_one(fmt, data, u32, k32, memmove):
                logger.debug(f"Clipboard privacy format {name} not set")
        except Exception as e:
            logger.debug(f"Clipboard privacy format {name} failed: {type(e).__name__}")
    placed = 0
    for fmt, data in formats.items():
        if _place_one(fmt, data, u32, k32, memmove):
            placed += 1
    return placed > 0


def _set_clipboard(formats: dict) -> bool:
    """Replace the clipboard with raw formats ({format id: bytes}), kept out
    of clipboard history and cloud sync."""
    # SetClipboardData fails when the clipboard was opened without a window.
    hwnd = _u32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    for _ in range(20):
        if _u32.OpenClipboard(hwnd):
            break
        time.sleep(0.01)
    else:
        if hwnd:
            _u32.DestroyWindow(hwnd)
        return False
    try:
        _u32.EmptyClipboard()
        return _place_formats(formats)
    finally:
        _u32.CloseClipboard()
        if hwnd:
            _u32.DestroyWindow(hwnd)


def paste_formats(formats: dict, settle: float = 0.4) -> bool:
    """Paste raw clipboard formats (an image as PNG + DIB) with Ctrl+V.

    Browsers and Electron apps read a pasted image asynchronously, so this
    waits `settle` seconds before anyone may change the clipboard again –
    otherwise the app could read the next paste's text instead."""
    _wait_modifiers_released()
    seq = user32.GetClipboardSequenceNumber()
    if not _set_clipboard(formats) or not _wait_clipboard_updated(seq):
        return False
    if not _send_ctrl_v():
        return False
    time.sleep(settle)
    return True


def _copied_from_empty_selection() -> bool:
    """VS Code copies the whole line on Ctrl+C without a selection and marks
    it in its editor metadata – which Chromium stores (UTF-16) inside its
    custom-MIME clipboard format."""
    raw = _clipboard_raw("Chromium Web Custom MIME Data Format")
    if not raw:
        return False
    data = raw.decode("utf-16-le", errors="ignore")
    return "vscode-editor-data" in data and '"isFromEmptySelection":true' in data


def copy_selection(timeout: float = 0.35) -> str:
    """Return the text currently selected in the focused app ('' if none).

    Sends Ctrl+C and watches the clipboard sequence number: apps leave the
    clipboard alone when nothing is selected, so no change means no
    selection – except editors that copy the current line then (VS Code,
    detected via its metadata). Like insert_text(), the clipboard is not
    restored afterwards – the command result replaces it a moment later."""
    # Held hotkey modifiers would turn Ctrl+C into e.g. Ctrl+Shift+C (dev
    # tools in browsers) – give a toggle-mode user time to let go.
    _wait_modifiers_released(1.0)
    try:
        seq = user32.GetClipboardSequenceNumber()
        if not _send_ctrl(VK_C):
            return ""
        if not _wait_clipboard_updated(seq, timeout):
            return ""
        # Some apps update the clipboard in several steps (formats) – read
        # after a short settle so we don't catch it half-written.
        time.sleep(0.03)
        if _copied_from_empty_selection():
            logger.info("Ctrl+C copied a whole line (no selection) – ignoring it")
            return ""
        return pyperclip.paste() or ""
    except Exception as e:
        logger.warning(f"Reading the selection failed: {e}")
        return ""


def insert_text(text: str, method: str = "auto") -> bool:
    """
    Insert text into the currently active input field.

    method:
      "auto" – clipboard + Ctrl+V (fast, formatting-safe); falls back to
               direct Unicode typing if the paste keystroke can't be sent.
      "type" – always type via SendInput (works in consoles/elevated targets
               that ignore Ctrl+V; never touches the clipboard).

    Note: in paste mode we do NOT restore the previous clipboard content.
    Restoring after a fixed delay races slow apps (browsers in particular) –
    sometimes the restore fires before the target has read the clipboard, and
    the transcribed text is silently lost. The clipboard simply ending up with
    the transcribed text is the expected dictation UX. The text is marked to
    stay out of Windows clipboard history and cloud sync (_place_formats).
    """
    # A dictated "neuer Absatz" alone is text, too.
    if not text or not (text.strip() or "\n" in text):
        logger.debug("Empty text, skipping insertion")
        return False

    # A still-held hotkey modifier would corrupt the paste keystroke or the
    # typed characters – wait (usually 0 ms) until everything is released.
    _wait_modifiers_released()

    try:
        if method == "type":
            success = _type_unicode(text)
            logger.info(f"Typed text ({success}): {len(text)} chars")
            return success

        seq = user32.GetClipboardSequenceNumber()
        # Windows text on the clipboard uses CRLF; some editors paste a bare
        # LF as nothing at all. Own Win32 path instead of pyperclip so the
        # text stays out of clipboard history and cloud sync.
        crlf = text.replace("\r\n", "\n").replace("\n", "\r\n")
        if not _set_clipboard({CF_UNICODETEXT: (crlf + "\0").encode("utf-16-le")}):
            logger.warning("Clipboard could not be set, typing instead")
            return _type_unicode(text)
        if not _wait_clipboard_updated(seq):
            logger.warning("Clipboard did not update in time, typing instead")
            return _type_unicode(text)
        success = _send_ctrl_v()
        if not success:
            logger.warning("Ctrl+V injection failed, typing instead")
            success = _type_unicode(text)
        logger.info(f"Inserted text ({success}): {len(text)} chars")
        return success
    except Exception as e:
        logger.error(f"Failed to insert text: {e}")
        return False
