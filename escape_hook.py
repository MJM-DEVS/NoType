"""
NoType – Esc cancels a dictation
A low-level keyboard hook (WH_KEYBOARD_LL) that is installed only while a
dictation records or is being processed. It swallows Esc so the app the user
dictates into never sees it (Esc would e.g. interrupt a running Claude Code
turn) and reports the press to the backend.

Why a hook and not an Electron global shortcut: in hold-to-speak mode the
user is still holding the hotkey's modifiers. A shortcut for plain Escape
doesn't fire with Ctrl+Shift held, and polling the key state can't stop
Ctrl+Shift+Esc from opening the Task Manager. The hook sees Esc before the
system's own hotkeys and can swallow it.

Exception: while the screenshot tool is in front (Win+Shift+S during a
dictation, for the AI context), Esc closes the screenshot, not the dictation.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import sys
import threading

from context_capture import SNIPPERS, foreground_process

logger = logging.getLogger("NoType.EscapeHook")

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
VK_ESCAPE = 0x1B

LRESULT = ctypes.c_ssize_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


# WINFUNCTYPE exists only on Windows; elsewhere the hook is never installed.
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
HOOKPROC = _FUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

if sys.platform.startswith("win"):
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
    _u32.SetWindowsHookExW.restype = wt.HHOOK
    _u32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
    _u32.CallNextHookEx.restype = LRESULT
    _u32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
    _u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    _u32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
    _u32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
    _k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    _k32.GetModuleHandleW.restype = wt.HMODULE
    _k32.GetCurrentThreadId.restype = wt.DWORD


class EscapeHook:
    """`arm()` while a dictation is active, `disarm()` once it is over.
    `on_escape` runs on the hook thread and must return quickly (Windows
    drops hooks that keep the keyboard waiting)."""

    def __init__(self, on_escape):
        self._on_escape = on_escape
        self._lock = threading.Lock()
        self._thread = None
        self._tid = 0
        self._armed = False
        self._esc_down = False
        self._esc_passed = False  # this press belongs to the screenshot tool
        self._proc = HOOKPROC(self._hook_proc)  # keep a reference: no GC while installed

    def arm(self):
        if not sys.platform.startswith("win"):
            return
        with self._lock:
            was_armed, self._armed = self._armed, True
            old = self._thread
            if old and old.is_alive() and (was_armed or self._esc_down):
                return  # already running, or lingering until Esc goes up
        if old and old.is_alive():
            old.join(0.5)  # quit already posted – let it finish first
        with self._lock:
            self._esc_down = False
            ready = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
            self._thread.start()
        ready.wait(1.0)

    def disarm(self):
        with self._lock:
            self._armed = False
            # Esc still held: keep swallowing its repeats and key-up, the hook
            # thread ends itself on release. Otherwise end it now.
            if self._tid and not self._esc_down:
                _u32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def _run(self, ready):
        msg = wt.MSG()
        # Creates this thread's message queue so PostThreadMessage can reach it.
        _u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        hook = _u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc,
                                      _k32.GetModuleHandleW(None), 0)
        if not hook:
            logger.warning(f"Esc hook not installed (error {ctypes.get_last_error()})")
            ready.set()
            return
        with self._lock:
            self._tid = _k32.GetCurrentThreadId()
            quit_now = not self._armed  # disarmed before we got here
        ready.set()
        try:
            if not quit_now:
                while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    pass
        finally:
            _u32.UnhookWindowsHookEx(hook)
            with self._lock:
                self._tid = 0

    def _hook_proc(self, code, wparam, lparam):
        if code == HC_ACTION:
            kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            # Injected presses count too: Esc remapped by AutoHotkey or
            # PowerToys arrives as injected input.
            if kb.vkCode == VK_ESCAPE:
                down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if (down and self._armed and not self._esc_down and not self._esc_passed
                        and foreground_process() in SNIPPERS):
                    self._esc_passed = True
                if self._esc_passed:
                    if not down:
                        self._esc_passed = False
                    return _u32.CallNextHookEx(None, code, wparam, lparam)
                if self._armed:
                    if down and not self._esc_down:
                        self._esc_down = True
                        try:
                            self._on_escape()
                        except Exception:
                            pass
                    elif not down:
                        self._esc_down = False
                    return 1
                if self._esc_down:
                    # Disarmed while Esc was held: swallow until release.
                    if not down:
                        self._esc_down = False
                        _u32.PostThreadMessageW(_k32.GetCurrentThreadId(), WM_QUIT, 0, 0)
                    return 1
        return _u32.CallNextHookEx(None, code, wparam, lparam)
