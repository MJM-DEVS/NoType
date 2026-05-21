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
VK_CONTROL = 0x11
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


def insert_text(text: str) -> bool:
    """
    Insert text into the currently active input field via clipboard + Ctrl+V.

    Note: we do NOT restore the previous clipboard content. Restoring after a
    fixed delay races slow apps (browsers in particular) – sometimes the restore
    fires before the target has read the clipboard, and the transcribed text
    is silently lost. The clipboard simply ending up with the transcribed text
    is the expected dictation UX.
    """
    if not text or not text.strip():
        logger.debug("Empty text, skipping insertion")
        return False

    try:
        pyperclip.copy(text)
        time.sleep(0.1)             # Give Windows a moment to update the clipboard
        success = _send_ctrl_v()
        logger.info(f"Inserted text ({success}): '{text[:50]}' ({len(text)} chars)")
        return success
    except Exception as e:
        logger.error(f"Failed to insert text: {e}")
        return False
