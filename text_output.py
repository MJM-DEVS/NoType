"""
NoType – Text Output
Inserts transcribed text into the currently active input field
using clipboard + Windows SendInput API for reliable Ctrl+V.
"""

import time
import logging
import ctypes
import ctypes.wintypes
import pyperclip

logger = logging.getLogger("NoType.TextOutput")

# ── Windows API ──
user32 = ctypes.windll.user32

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
    for ch in text:
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
    the transcribed text is the expected dictation UX.
    """
    if not text or not text.strip():
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
        pyperclip.copy(text)
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
