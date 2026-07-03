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


def _send_ctrl_v():
    """Simulate Ctrl+V using Windows SendInput API."""
    inputs = (INPUT * 4)()

    # Ctrl down
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].ki.wVk = VK_CONTROL

    # V down
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].ki.wVk = VK_V

    # V up
    inputs[2].type = INPUT_KEYBOARD
    inputs[2].ki.wVk = VK_V
    inputs[2].ki.dwFlags = KEYEVENTF_KEYUP

    # Ctrl up
    inputs[3].type = INPUT_KEYBOARD
    inputs[3].ki.wVk = VK_CONTROL
    inputs[3].ki.dwFlags = KEYEVENTF_KEYUP

    result = user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    if result != 4:
        logger.warning(f"SendInput returned {result}, expected 4 (error: {ctypes.GetLastError()})")
    return result == 4


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
            logger.info(f"Typed text ({success}): '{text[:50]}' ({len(text)} chars)")
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
        logger.info(f"Inserted text ({success}): '{text[:50]}' ({len(text)} chars)")
        return success
    except Exception as e:
        logger.error(f"Failed to insert text: {e}")
        return False
