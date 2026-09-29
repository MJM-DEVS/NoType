"""
NoType – Correction learning (dictionary, stage 2)

After a dictation is inserted, the focused text field is read for a short
while through Windows UI Automation. When the user fixes a misrecognized word
inside the dictated text ("Kuber Netis" → "Kubernetes"), the pair is offered
as a dictionary entry – the Electron side asks before anything is learned.

Privacy: reading stays local and ends with the watch (focus leaves the field,
a new dictation starts, or WATCH_S passes). Password fields are never read,
and only the dictated part of the field is compared.
"""

import ctypes
import difflib
import logging
import re
import threading
import time

logger = logging.getLogger("NoType.Corrections")

WATCH_S = 60.0          # stop watching this long after the insertion
POLL_S = 0.6            # field read interval
STABLE_S = 2.0          # suggest once the text stopped changing for this long
MAX_TEXT = 20000        # chars read from a field (long documents)
MAX_PHRASE_WORDS = 3    # longest heard/meant phrase that counts as a fix
MAX_EDITS = 4           # more changes than this = a rewrite, not a fix
MAX_SUGGESTIONS = 3
MIN_SIMILARITY = 0.5

# Words incl. inner hyphens/apostrophes/dots ("E-Mail", "don't", "v2.6").
_WORD_RE = re.compile(r"[^\W_]+(?:[-'’.][^\W_]+)*")


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix(a: str, b: str, limit: int) -> int:
    n = min(len(a), len(b), limit)
    i = 0
    while i < n and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def _is_fix(heard: str, meant: str) -> bool:
    """A correction of a misrecognized word – not a rewrite, not a
    grammar/sentence-start touch-up."""
    nh, nm = _norm(heard), _norm(meant)
    if len(nm) < 3 or not any(ch.isalpha() for ch in nm):
        return False
    if nh == nm:
        if heard.lower() != meant.lower():
            return True  # separators: "E Mail" → "E-Mail"
        # Case only: brand/acronym spellings ("whatsapp" → "WhatsApp",
        # "api" → "API"), not a capitalized sentence start ("das" → "Das").
        return any(ch.isupper() for ch in meant[1:])
    # Names and terms carry a capital or digit; a changed lowercase word
    # ("wie" → "die") is grammar in context – nothing to learn globally.
    if not any(ch.isupper() or ch.isdigit() for ch in meant):
        return False
    return difflib.SequenceMatcher(None, nh, nm).ratio() >= MIN_SIMILARITY


def find_corrections(inserted: str, before: str, after: str):
    """Corrections the user made inside the dictated text.

    `before` is the field right after the insertion (it contains `inserted`),
    `after` a later reading. Returns [(heard, meant), ...], [] when the
    dictated text is unchanged or only edited in other ways, and None when
    the dictated text is mostly gone (field cleared, message sent) – the
    caller keeps its previous answer then."""
    start = before.rfind(inserted)
    if start < 0 or not inserted.strip():
        return None
    end = start + len(inserted)
    # Text around the dictation that survived unchanged frames the region to
    # compare; everything the user typed before/after it stays outside.
    p = min(_common_prefix(before, after), start)
    s = min(_common_suffix(before, after, len(after) - p), len(before) - end)
    old_region = before[p:len(before) - s]
    new_region = after[p:len(after) - s]

    old_tok = [m for m in _WORD_RE.finditer(old_region)]
    new_tok = [m for m in _WORD_RE.finditer(new_region)]
    # Only words of the dictation itself count.
    lo, hi = start - p, end - p
    dictated = [i for i, m in enumerate(old_tok) if m.start() >= lo and m.end() <= hi]
    if not dictated:
        return None

    sm = difflib.SequenceMatcher(None, [m.group() for m in old_tok],
                                 [m.group() for m in new_tok], autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    first, last = dictated[0], dictated[-1] + 1
    inside = [op for op in ops if op[1] < last and op[2] > first
              or (op[1] == op[2] and first < op[1] < last)]
    removed = sum(i2 - i1 for tag, i1, i2, _, _ in inside if tag in ("delete", "replace"))
    if removed > len(dictated) / 2:
        return None
    if len(inside) > MAX_EDITS:
        return []

    out = []
    for tag, i1, i2, j1, j2 in inside:
        if tag != "replace" or i1 < first or i2 > last:
            continue
        if i2 - i1 > MAX_PHRASE_WORDS or j2 - j1 > MAX_PHRASE_WORDS:
            continue
        heard = old_region[old_tok[i1].start():old_tok[i2 - 1].end()]
        meant = new_region[new_tok[j1].start():new_tok[j2 - 1].end()]
        if _is_fix(heard, meant) and (heard, meant) not in out:
            out.append((heard, meant))
    return out[:MAX_SUGGESTIONS]


# ── UI Automation ────────────────────────────────────────────────────

_user32 = ctypes.windll.user32
_UIA = None


def _uia_module():
    global _UIA
    if _UIA is None:
        try:
            from comtypes.gen import UIAutomationClient as mod
        except ImportError:
            import comtypes.client
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as mod
        _UIA = mod
    return _UIA


class _FieldReader:
    """Reads the focused element's text – per thread (COM apartment)."""

    def __init__(self):
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            self._owns_com = True
        except OSError:
            # The thread that first imports comtypes is already initialized
            # (STA) by that import – fine for plain polling calls.
            self._owns_com = False
        uia = _uia_module()
        self.m = uia
        self.auto = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
        try:
            # A hung target app must not hang the watcher for UIA's 20 s default.
            a2 = self.auto.QueryInterface(uia.IUIAutomation2)
            a2.TransactionTimeout = 1500
            a2.ConnectionTimeout = 1500
        except Exception:
            pass

    def focused(self):
        el = self.auto.GetFocusedElement()
        if not el or el.CurrentIsPassword:
            return None
        return el

    def text(self, el) -> str | None:
        m = self.m
        try:
            pat = el.GetCurrentPattern(m.UIA_ValuePatternId)
            if pat:
                vp = pat.QueryInterface(m.IUIAutomationValuePattern)
                value = vp.CurrentValue
                if value:
                    return value[:MAX_TEXT]
        except Exception:
            pass
        try:
            pat = el.GetCurrentPattern(m.UIA_TextPatternId)
            if pat:
                tp = pat.QueryInterface(m.IUIAutomationTextPattern)
                return tp.DocumentRange.GetText(MAX_TEXT)
        except Exception:
            pass
        return None

    def same(self, a, b) -> bool:
        try:
            return bool(self.auto.CompareElements(a, b))
        except Exception:
            return False

    def close(self):
        import comtypes
        self.auto = None
        if self._owns_com:
            comtypes.CoUninitialize()


class CorrectionWatcher:
    """One watch at a time; `watch()` replaces a running one."""

    def __init__(self, on_suggestions):
        self._on_suggestions = on_suggestions
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def cancel(self):
        self._cancel.set()

    def watch(self, inserted: str, known=()):
        """Start watching the focused field for fixes of `inserted`. `known`:
        (heard, meant) pairs already in the dictionary – not suggested again."""
        with self._lock:
            self._cancel.set()
            self._cancel = cancel = threading.Event()
        threading.Thread(target=self._run, args=(inserted, cancel, set(known)),
                         daemon=True, name="correction-watch").start()

    def _run(self, inserted: str, cancel: threading.Event, known: set):
        reader = None
        try:
            reader = _FieldReader()
            # Let the paste land first. Chromium-based apps (browsers, Slack,
            # Teams, VS Code) build their accessibility tree only on the
            # first request – give them a few tries.
            el = before = None
            for _ in range(4):
                if cancel.wait(0.35):
                    return
                el = reader.focused()
                before = reader.text(el) if el is not None else None
                if before and inserted in before:
                    break
            else:
                logger.debug("Field not readable or dictation not found – no watch")
                return
            hwnd = _user32.GetForegroundWindow()
            deadline = time.monotonic() + WATCH_S
            last, last_change, current = before, None, []

            def emit(found):
                found = [f for f in found if f not in known]
                if found and not cancel.is_set():
                    logger.info(f"Correction(s) spotted: {len(found)}")
                    self._on_suggestions(found)

            while not cancel.wait(POLL_S):
                now = time.monotonic()
                focus = reader.focused()
                if (now > deadline or _user32.GetForegroundWindow() != hwnd
                        or focus is None or not reader.same(focus, el)):
                    emit(current)
                    return
                text = reader.text(el)
                if text is None:
                    continue
                if text != last:
                    last, last_change = text, now
                    found = find_corrections(inserted, before, text)
                    if found is not None:
                        current = found
                elif last_change and current and now - last_change >= STABLE_S:
                    emit(current)
                    return
        except Exception as e:
            logger.debug(f"Correction watch ended: {e}")
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass
