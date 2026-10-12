"""
NoType – AI context: what you copy comes along

Dictating into an AI app or a terminal, whatever the user copies while the
recording runs (Ctrl+C, or a screenshot with Win+Shift+S) is attached to the
dictation. So is a copy from up to a minute before the start: in
hold-to-speak the hands are on the hotkey, so it's snip first, then speak.

  chat apps (ChatGPT, Claude, Gemini, … as app or browser tab): images are
      pasted like Ctrl+V, copied text is quoted under the sentence.
  terminals (Claude Code, Codex, …): images are saved as PNG and their paths
      go in front of the sentence; the CLI reads them from disk. Long or
      multi-line text goes to a .txt file the same way (a multi-line paste
      would run each line in a plain shell).

Browser tabs whose title doesn't name an AI site (ChatGPT titles its tabs
after the chat) and code editors take what is copied during the recording,
but from the minute before only images: an earlier text copy there is too
often unrelated. Every other app (Word, Outlook, WhatsApp, …) ignores the
clipboard.

The clipboard is watched by its sequence number; contents are read only
while a dictation runs (or once at its start, for the minute before). None
of it goes to the log. Saved files live in %APPDATA%/NoType/attachments and
are deleted after a day.
"""

import base64
import collections
import ctypes
import ctypes.wintypes as wt
import hashlib
import logging
import math
import os
import re
import struct
import sys
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np

from text_output import insert_text, paste_formats

logger = logging.getLogger("NoType.Context")

LOOKBACK_S = 60.0        # a copy this recent at the start still counts
POLL_IDLE_S = 0.25       # sequence-number poll between dictations
POLL_REC_S = 0.08        # … and while one records
SETTLE_S = 0.06          # let the source app finish writing all its formats
OWN_GRACE_S = 0.5        # our Ctrl+C (command mode) can land a bit late
MAX_ITEMS = 5
MAX_IMAGES = 3
MAX_TEXT = 20000         # chars kept from one copy
INLINE_MAX = 160         # terminal: shorter one-line text goes inline
KEEP_FILES_S = 24 * 3600
THUMB_W, THUMB_H = 96, 64  # overlay chip thumbnail (2× its CSS size)

CF_UNICODETEXT = 13
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
BI_RGB, BI_BITFIELDS = 0, 3

# Lower-case exe names.
AI_APPS = {
    "chatgpt.exe", "claude.exe", "perplexity.exe", "copilot.exe", "m365copilot.exe",
    "lm studio.exe", "msty.exe", "jan.exe", "chatbox.exe", "cherry studio.exe",
    "anythingllm.exe", "gpt4all.exe", "poe.exe",
}
TERMINALS = {
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe", "powershell.exe",
    "pwsh.exe", "wezterm-gui.exe", "alacritty.exe", "mintty.exe", "hyper.exe", "tabby.exe",
    "warp.exe", "rio.exe", "contour.exe",
}
EDITORS = {
    "code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe", "zed.exe", "trae.exe",
    "kiro.exe", "void.exe", "antigravity.exe", "positron.exe", "fleet.exe", "devenv.exe",
    "idea64.exe", "pycharm64.exe", "webstorm64.exe", "goland64.exe", "rider64.exe",
    "clion64.exe", "phpstorm64.exe", "rubymine64.exe", "datagrip64.exe", "studio64.exe",
}
BROWSERS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe",
    "arc.exe", "zen.exe", "librewolf.exe", "waterfox.exe", "floorp.exe", "thorium.exe",
    "chromium.exe", "comet.exe", "dia.exe", "sidekick.exe",
}
# Store apps run inside this host; their window title is the app name.
APP_HOSTS = {"applicationframehost.exe"}
SNIPPERS = {"screenclippinghost.exe", "snippingtool.exe"}
# A window title segment equal to one of these marks an AI site or app
# ("Neuer Chat - Claude", "Google Gemini", "ChatGPT").
AI_TITLES = {
    "chatgpt", "claude", "gemini", "google gemini", "perplexity", "copilot",
    "microsoft copilot", "microsoft 365 copilot", "grok", "deepseek", "le chat",
    "mistral ai", "ai studio", "google ai studio", "notebooklm", "poe", "t3 chat",
    "huggingchat", "codex", "meta ai", "duck.ai", "kimi", "qwen chat", "phind",
}
_TITLE_SPLIT = re.compile(r"\s+[-–—|·]\s+|:\s+")
_EDGE_MORE = re.compile(r"\s+(?:and|und)\s+\d+\s+\S+(?:\s+\S+)?$", re.I)  # "… und 3 weitere Seiten"
_REFERENCE = re.compile(r"^(?:https?://|file://|data:image/|[a-z]:\\|\\\\)\S*$", re.I)

_WIN = sys.platform.startswith("win")
if _WIN:
    # Own handles: pyperclip sets its own argtypes on ctypes.windll.
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32.GetClipboardSequenceNumber.restype = wt.DWORD
    _u32.OpenClipboard.argtypes = [wt.HWND]
    _u32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
    _u32.GetClipboardData.argtypes = [wt.UINT]
    _u32.GetClipboardData.restype = wt.HANDLE
    _u32.GetClipboardOwner.restype = wt.HWND
    _u32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
    _u32.RegisterClipboardFormatW.restype = wt.UINT
    _u32.GetForegroundWindow.restype = wt.HWND
    _u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _u32.GetWindowThreadProcessId.restype = wt.DWORD
    _u32.GetWindowTextLengthW.argtypes = [wt.HWND]
    _u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _k32.GlobalLock.argtypes = [wt.HGLOBAL]
    _k32.GlobalLock.restype = ctypes.c_void_p
    _k32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    _k32.GlobalSize.argtypes = [wt.HGLOBAL]
    _k32.GlobalSize.restype = ctypes.c_size_t
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    CF_PNG = _u32.RegisterClipboardFormatW("PNG")
else:
    CF_PNG = 0


# ── Windows and processes ─────────────────────────────────────────

def _window_pid(hwnd) -> int:
    if not hwnd:
        return 0
    pid = wt.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid: int) -> str:
    """Lower-case exe name of a process, '' if it can't be read."""
    if not pid:
        return ""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if not _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        _k32.CloseHandle(h)


def _window_title(hwnd) -> str:
    n = _u32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def foreground_process() -> str:
    """Exe name (lower case) of the window in front, '' if unknown."""
    if not _WIN:
        return ""
    try:
        return _process_name(_window_pid(_u32.GetForegroundWindow()))
    except Exception:
        return ""


def foreground_window() -> tuple[str, str]:
    """(exe name in lower case, title) of the window in front, '' if unknown."""
    if not _WIN:
        return "", ""
    try:
        hwnd = _u32.GetForegroundWindow()
        return _process_name(_window_pid(hwnd)), _window_title(hwnd)
    except Exception:
        return "", ""


# ── Where the dictation goes ──────────────────────────────────────

@dataclass(frozen=True)
class Target:
    kind: str   # chat | browser | terminal | editor
    app: str    # exe name, for the log

    @property
    def paths(self) -> bool:
        """Attachments go in as file paths (terminals, editors)."""
        return self.kind in ("terminal", "editor")

    @property
    def early_text(self) -> bool:
        """Text copied before the start counts too (not just images)."""
        return self.kind in ("chat", "terminal")


def is_ai_title(title: str) -> bool:
    for seg in _TITLE_SPLIT.split(title or ""):
        seg = _EDGE_MORE.sub("", seg)
        seg = re.sub(r"^\(\d+\)\s*", "", seg)          # "(2) ChatGPT": unread count
        seg = seg.strip(" \t‎‏*•●✳").lower()
        if seg in AI_TITLES:
            return True
    return False


def classify(app: str, title: str):
    """The Target for a window of `app` (exe name) titled `title`, or None
    when the clipboard should stay out of it."""
    app = (app or "").lower()
    if app in AI_APPS:
        return Target("chat", app)
    if app in TERMINALS:
        return Target("terminal", app)
    if app in BROWSERS:
        return Target("chat" if is_ai_title(title) else "browser", app)
    if app in EDITORS:
        return Target("editor", app)
    if app in APP_HOSTS and is_ai_title(title):
        return Target("chat", app)
    return None


def classify_foreground():
    if not _WIN:
        return None
    try:
        hwnd = _u32.GetForegroundWindow()
        return classify(_process_name(_window_pid(hwnd)), _window_title(hwnd))
    except Exception as e:
        logger.debug(f"Classifying the foreground window failed: {e}")
        return None


# ── Clipboard ─────────────────────────────────────────────────────

def _seq() -> int:
    return _u32.GetClipboardSequenceNumber() if _WIN else 0


@contextmanager
def _clipboard():
    for _ in range(20):  # another app may hold it for a moment
        if _u32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        yield False
        return
    try:
        yield True
    finally:
        _u32.CloseClipboard()


def _get(fmt) -> bytes | None:
    """Bytes of one format; the clipboard must be open."""
    if not fmt or not _u32.IsClipboardFormatAvailable(fmt):
        return None
    h = _u32.GetClipboardData(fmt)
    p = _k32.GlobalLock(h) if h else None
    if not p:
        return None
    try:
        return ctypes.string_at(p, _k32.GlobalSize(h))
    finally:
        _k32.GlobalUnlock(h)


@dataclass
class _Clip:
    kind: str                 # text | image
    text: str = ""
    formats: dict = field(default_factory=dict)
    owner: str = ""           # exe that put it there


def _read_clipboard():
    """What the clipboard holds now as a _Clip, None if nothing usable.
    Text wins over a picture (Excel puts both); an image wins over text that
    is only its address."""
    with _clipboard() as ok:
        if not ok:
            return None
        owner = _process_name(_window_pid(_u32.GetClipboardOwner()))
        raw = _get(CF_UNICODETEXT)
        text = raw.decode("utf-16-le", "ignore").split("\0", 1)[0] if raw else ""
        has_image = any(_u32.IsClipboardFormatAvailable(f) for f in (CF_DIB, CF_PNG) if f)
        if text.strip() and not (has_image and _REFERENCE.match(text.strip())):
            return _Clip("text", text=text[:MAX_TEXT], owner=owner)
        if not has_image:
            return None
        formats = {f: data for f in (CF_PNG, CF_DIB) if f and (data := _get(f))}
    return _Clip("image", formats=formats, owner=owner) if formats else None


# ── Images (no PIL: numpy + zlib) ─────────────────────────────────

def dib_to_pixels(dib: bytes):
    """CF_DIB bytes → uint8 array (h, w, 3) RGB or (h, w, 4) RGBA; None for
    layouts screenshots don't use (palettes, 16 bit, compressed)."""
    if not dib or len(dib) < 40:
        return None
    size, w, h, _planes, bpp, comp = struct.unpack_from("<IiiHHI", dib)
    clr_used = struct.unpack_from("<I", dib, 32)[0]
    if w <= 0 or h == 0 or w > 16384 or abs(h) > 16384 or bpp not in (24, 32):
        return None
    offset = size + clr_used * 4
    if comp == BI_BITFIELDS:
        if bpp != 32 or struct.unpack_from("<III", dib, 40) != (0xFF0000, 0xFF00, 0xFF):
            return None
        if size == 40:
            offset += 12  # masks follow a plain BITMAPINFOHEADER
    elif comp != BI_RGB:
        return None
    top_down, h = h < 0, abs(h)
    stride = (w * bpp + 31) // 32 * 4
    if len(dib) < offset + stride * h:
        return None
    rows = np.frombuffer(dib, np.uint8, count=stride * h, offset=offset).reshape(h, stride)
    px = rows[:, :w * (bpp // 8)].reshape(h, w, bpp // 8)
    if not top_down:
        px = px[::-1]
    if bpp == 32:
        alpha = px[..., 3]
        # Most apps leave the 4th byte at 0 – that means opaque, not invisible.
        if alpha.any() and not (alpha == 255).all():
            return np.ascontiguousarray(px[..., [2, 1, 0, 3]])
    return np.ascontiguousarray(px[..., [2, 1, 0]])


def _png_chunk(tag: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))


def encode_png(px, level: int = 6) -> bytes:
    """uint8 RGB/RGBA array → PNG bytes. Every row uses the Up filter, which
    suits screenshots (large flat areas) and costs one numpy subtraction."""
    h, w, c = px.shape
    raw = px.reshape(h, w * c)
    rows = np.empty((h, w * c + 1), np.uint8)
    rows[:, 0] = 2
    rows[0, 0] = 0
    rows[0, 1:] = raw[0]
    np.subtract(raw[1:], raw[:-1], out=rows[1:, 1:])  # wraps mod 256 as PNG wants
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6 if c == 4 else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(rows.tobytes(), level)) + _png_chunk(b"IEND", b""))


def thumbnail(px) -> str:
    """Small PNG data URL of the image for the overlay chip."""
    h, w = px.shape[:2]
    k = max(1, math.ceil(max(w / THUMB_W, h / THUMB_H)))
    step = max(1, k // 3)                 # thin out first: a 4K mean would need GBs
    small = px[::step, ::step]
    b = math.ceil(k / step)              # step·b ≥ k keeps it inside the box
    hh, ww = small.shape[0] // b * b, small.shape[1] // b * b
    if hh and ww:
        small = small[:hh, :ww].reshape(hh // b, b, ww // b, b, -1).mean(axis=(1, 3))
    small = np.ascontiguousarray(small, dtype=np.uint8)
    return "data:image/png;base64," + base64.b64encode(encode_png(small, 9)).decode("ascii")


# ── Attachments ───────────────────────────────────────────────────

@dataclass
class Attachment:
    kind: str                 # image | text
    seq: int                  # clipboard sequence number it came from
    source: str = ""          # screenshot | image | text
    text: str = ""
    formats: dict = field(default_factory=dict)  # raw clipboard formats (chat paste)
    path: str = ""            # saved file (terminals)
    thumb: str = ""
    digest: bytes = b""       # content hash (duplicates within one dictation)

    def info(self) -> dict:
        """What the overlay chip shows."""
        if self.kind == "image":
            return {"kind": "image", "source": self.source, "thumb": self.thumb}
        line = " ".join(self.text.split())
        return {"kind": "text", "preview": line[:60], "lines": self.text.strip().count("\n") + 1}


class Session:
    """The attachments of one dictation."""

    def __init__(self, target: Target):
        self.target = target
        self.items: list[Attachment] = []
        self.open = True
        self._digests = set()
        self._workers: list[threading.Thread] = []


class ContextCapture:
    """Watches the clipboard and collects copies for the running dictation.
    `on_attachment(info)` runs on a worker thread for every new attachment."""

    def __init__(self, folder: str, on_attachment):
        self._folder = folder
        self._on_attachment = on_attachment
        self._lock = threading.Lock()
        self._seq = _seq()
        self._last_copy = None    # (seq, monotonic time) of the newest user copy
        self._own_depth = 0
        self._own_until = 0.0
        self._consumed = collections.deque(maxlen=32)  # seqs already sent along once
        # seq → content hash of every copy read for the current dictation. A
        # copy made twice goes along once; consume() retires both seqs so the
        # duplicate doesn't come back as the next dictation's look-back.
        self._seen = {}
        self._session = None
        self._cleaned_at = 0.0
        if _WIN:
            threading.Thread(target=self._watch, name="clipboard-watch", daemon=True).start()

    # ── NoType's own clipboard writes ──
    @contextmanager
    def own_writes(self):
        """Clipboard changes inside this block (our paste, the Ctrl+C of
        command mode) are not the user's copies."""
        with self._lock:
            self._own_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._own_depth -= 1
                self._own_until = time.monotonic() + OWN_GRACE_S
                self._seq = _seq()
                self._last_copy = None

    def _is_own(self) -> bool:
        if self._own_depth or time.monotonic() < self._own_until:
            return True
        try:
            return _window_pid(_u32.GetClipboardOwner()) == os.getpid()
        except Exception:
            return False

    def _watch(self):
        while True:
            time.sleep(POLL_REC_S if self._session else POLL_IDLE_S)
            try:
                seq = _seq()
                if seq == self._seq:
                    continue
                own = self._is_own()
                with self._lock:
                    if seq == self._seq:
                        continue
                    self._seq = seq
                    if own:
                        self._last_copy = None
                        continue
                    self._last_copy = (seq, time.monotonic())
                    session = self._session if self._session and self._session.open else None
                if session:
                    self._spawn(session, seq, early=False)
            except Exception as e:
                logger.debug(f"Clipboard watch: {e}")

    # ── Dictation lifecycle ──
    def begin(self, target: Target) -> Session:
        """A dictation into `target` starts: collect from now on, plus the
        last copy if it is recent and wasn't sent along before."""
        session = Session(target)
        with self._lock:
            self._session = session
            self._seen = {}
            last = self._last_copy
            fresh = (last and time.monotonic() - last[1] <= LOOKBACK_S
                     and last[0] not in self._consumed)
        if fresh:
            self._spawn(session, last[0], early=True)
        return session

    def close(self, session: Session):
        """Recording ended: later copies don't count."""
        session.open = False
        with self._lock:
            if self._session is session:
                self._session = None

    def finish(self, session: Session, timeout: float = 2.0) -> list:
        """The attachments, once copies still being read are in."""
        self.close(session)
        deadline = time.monotonic() + timeout
        for t in list(session._workers):
            t.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            return list(session.items)

    def consume(self, items):
        """These went out with a dictation; the next one won't pick them up."""
        with self._lock:
            sent = {item.digest for item in items}
            for item in items:
                self._consumed.append(item.seq)
            for seq, digest in self._seen.items():
                if digest in sent and seq not in self._consumed:
                    self._consumed.append(seq)

    # ── Reading a copy ──
    def _spawn(self, session: Session, seq: int, early: bool):
        t = threading.Thread(target=self._collect, args=(session, seq, early), daemon=True)
        session._workers.append(t)
        t.start()

    def _collect(self, session: Session, seq: int, early: bool):
        try:
            if not early:
                time.sleep(SETTLE_S)
            clip = _read_clipboard()
            # Changed again meanwhile: the newer copy gets its own turn.
            if clip is None or _seq() != seq:
                return
            if early and clip.kind == "text" and not session.target.early_text:
                return
            digest = hashlib.sha1(clip.text.encode("utf-8", "replace") if clip.kind == "text"
                                  else b"".join(clip.formats.values())).digest()
            with self._lock:
                self._seen[seq] = digest
                if digest in session._digests or len(session.items) >= MAX_ITEMS:
                    return
                if clip.kind == "image" and sum(a.kind == "image" for a in session.items) >= MAX_IMAGES:
                    return
                session._digests.add(digest)
            item = self._attachment(clip, seq, session.target)
            if item is None:
                return
            item.digest = digest
            with self._lock:
                session.items.append(item)
            logger.info(f"Attachment: {item.kind} ({'before the start' if early else 'while recording'})")
            try:
                self._on_attachment(item.info())
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Reading a copy failed: {e}")

    def _attachment(self, clip: _Clip, seq: int, target: Target):
        if clip.kind == "text":
            text = clip.text
            path = ""
            if target.paths and ("\n" in text.strip() or len(text.strip()) > INLINE_MAX):
                path = self._save(text.encode("utf-8"), "txt")
            return Attachment("text", seq, "text", text=text, path=path)
        px = dib_to_pixels(clip.formats.get(CF_DIB))
        thumb = thumbnail(px) if px is not None else ""
        source = "screenshot" if clip.owner in SNIPPERS else "image"
        if not target.paths:
            return Attachment("image", seq, source, formats=clip.formats, thumb=thumb)
        png = clip.formats.get(CF_PNG) or (encode_png(px) if px is not None else None)
        if not png:
            logger.info("Image format not supported, not attached")
            return None
        return Attachment("image", seq, source, path=self._save(png, "png"), thumb=thumb)

    def _save(self, data: bytes, ext: str) -> str:
        os.makedirs(self._folder, exist_ok=True)
        self._cleanup()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while os.path.exists(path := os.path.join(self._folder, f"notype-{stamp}-{n}.{ext}")):
            n += 1
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _cleanup(self):
        now = time.time()
        if now - self._cleaned_at < 3600:
            return
        self._cleaned_at = now
        try:
            for name in os.listdir(self._folder):
                p = os.path.join(self._folder, name)
                if name.startswith("notype-") and now - os.path.getmtime(p) > KEEP_FILES_S:
                    os.remove(p)
        except OSError as e:
            logger.debug(f"Attachment cleanup: {e}")


# ── Insertion ─────────────────────────────────────────────────────

def _path_arg(path: str) -> str:
    return f'"{path}"' if " " in path else path


def _quote(text: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in text.strip("\r\n").splitlines())


def compose(text: str, items, target: Target) -> str:
    """The text that goes in with the attachments (images of a chat target
    are pasted separately)."""
    texts = [a for a in items if a.kind == "text"]
    if target.paths:
        front = [_path_arg(a.path) for a in items if a.path]
        inline = "".join(f' "{a.text.strip()}"' for a in texts if not a.path)
        return " ".join(front + [text]) + inline
    return text + "".join("\n\n" + _quote(a.text) for a in texts)


def insert_with_context(text: str, items, target: Target, method: str = "auto") -> bool:
    """Insert a dictation with its attachments into the focused app."""
    if not target.paths:
        for item in items:
            if item.kind == "image" and item.formats:
                if not paste_formats(item.formats):
                    logger.warning("Pasting an image failed")
    composed = compose(text, items, target)
    # Typed newlines are Enter presses – in a chat that sends the message.
    if "\n" in composed:
        method = "auto"
    return insert_text(composed, method=method)
