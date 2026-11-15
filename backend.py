"""
NoType – Python Backend (IPC via stdin/stdout)
Receives JSON commands from Electron, sends JSON responses.
Handles audio recording, transcription, and text insertion.
"""

import sys
import os
import io

# The speech engine process is this same program with one flag. It sets up its
# own stdio and loads nothing of what follows (see transcribe_worker.py).
if "--transcribe-worker" in sys.argv:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import transcribe_worker
    transcribe_worker.main()
    os._exit(0)

# ── Recover stdio in PyInstaller `console=False` mode ───────────────
# Built with the Windows GUI subsystem, so PyInstaller's bootloader leaves
# sys.stdin/stdout/stderr as None. When Electron spawns us with
# `stdio: ['pipe','pipe','pipe']` the underlying file descriptors 0/1/2 are
# valid OS pipes — we just need to wire Python's stdio objects to them.
# If we were launched without a parent that piped these (e.g. user double-
# clicks backend.exe), os.fdopen raises and we leave the streams None;
# `run()` then exits cleanly because the stdin read loop yields nothing.
for _fd, _name, _mode in [(0, "stdin", "r"), (1, "stdout", "w"), (2, "stderr", "w")]:
    if getattr(sys, _name, None) is None:
        try:
            _raw = os.fdopen(_fd, _mode + "b", buffering=0, closefd=False)
            setattr(sys, _name,
                    io.TextIOWrapper(_raw, encoding="utf-8", errors="replace",
                                     line_buffering=(_mode == "w")))
        except Exception:
            pass  # No usable pipe – stay None; main loop will exit gracefully.

# Force UTF-8 on any pre-existing streams (e.g. interpreter dev runs).
for _stream_name in ("stdout", "stdin", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import json
import logging
import logging.handlers
import threading
import time
import ctypes

# Ensure our directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_recorder import (AudioRecorder, refresh_devices, input_devices,
                            find_input_device, input_device_name)
from transcriber import BUILTIN_REPOS, JobCancelled, MODEL_CATALOG, normalize_languages
from speech_engine import SpeechEngine
import audio_spool
from text_output import insert_text, copy_selection, copy_text
from config import load_config, save_config, CONFIG_DIR
from postprocess import (clean_transcript, warm_up, warm_up_async,
                         reset_availability_cache, rule_clean, run_command,
                         DEFAULT_LLM_MODEL)
import dictionary
import spoken_punctuation
import ollama_manager
from correction_watch import CorrectionWatcher
from escape_hook import EscapeHook
from context_capture import (ContextCapture, classify_foreground, foreground_window,
                             insert_with_context)
import agent_webhook
from output_mute import OutputMuter

# Log to file only (stdout is for IPC). Rotate so the log file never grows unbounded.
# CONFIG_DIR is %APPDATA%/NoType — same directory the frontend uses, survives rebuilds.
# Never log dictated text: people attach this file to public GitHub issues.
LOG_FILE = os.path.join(CONFIG_DIR, "notype.log")
LOG_BACKUPS = 3
# Versions up to 2.7 logged short snippets of every dictation. Delete those
# files once, so the old snippets don't linger until rotation pushes them out.
_LOG_PURGED = os.path.join(CONFIG_DIR, ".log-purged")
_OLD_LOGS = [LOG_FILE] + [f"{LOG_FILE}.{i}" for i in range(1, LOG_BACKUPS + 1)]
if not os.path.exists(_LOG_PURGED):
    for _path in _OLD_LOGS:
        try:
            os.remove(_path)
        except OSError:
            pass
    # A locked file stays behind; then try again on the next start.
    if not any(os.path.exists(_path) for _path in _OLD_LOGS):
        try:
            open(_LOG_PURGED, "w").close()
        except OSError:
            pass
_log_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=LOG_BACKUPS, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[_log_handler],
)
logger = logging.getLogger("NoType.Backend")

# A dictation can take a while. If the user was in another window by the time
# the text is ready, a paste would land there: it goes to the clipboard instead.
FOCUS_GUARD_SECONDS = 4.0


def _foreground_hwnd() -> int:
    return ctypes.windll.user32.GetForegroundWindow() if sys.platform.startswith("win") else 0


def _safe_str(s: str) -> str:
    """Round-trip a string through UTF-8 to drop any stray surrogate/cp1252 bytes
    that occasionally creep in from sounddevice device names or transcription output."""
    if not isinstance(s, str):
        return str(s)
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


class Backend:
    def __init__(self):
        self.config = load_config()
        self.recorder = None
        self.engine = SpeechEngine(
            on_ready=self._on_engine_ready,
            on_status=lambda text: self.send("status", {"message": text}),
            on_fallback=lambda: self.send("engine_fallback"),
            on_failed=lambda text: self.send("error", {"message": f"Model load failed: {text}"}),
            is_recording=lambda: bool(self.recorder and self.recorder.is_recording),
        )
        # Rescue copy of the running dictation, and the ids of all recordings
        # still being worked on (the rest of the folder are leftovers).
        self._spool = None
        self._spools_in_work = set()
        self._rescue_lock = threading.Lock()
        self._amplitude_thread = None
        self._running = True
        # Dictionary stage 2: fixes the user types right after a dictation
        # are offered as dictionary entries (Electron asks first).
        self.corrections = CorrectionWatcher(
            lambda items: self.send("dictionary_suggestions", {
                "items": [{"from": heard, "to": meant} for heard, meant in items]}))
        # Esc drops the running dictation (recording or still processing).
        self._cancel_requested = False
        self.esc_hook = EscapeHook(self._on_escape)
        # Optional: speakers muted while recording.
        self.muter = OutputMuter(os.path.join(CONFIG_DIR, ".muted-by-notype"))
        # AI context: copies made around a dictation into an AI app or a
        # terminal go along with it (overlay chips via attachment_added).
        self.context = ContextCapture(os.path.join(CONFIG_DIR, "attachments"),
                                      lambda info: self.send("attachment_added", info))
        self._context = None  # Session of the running dictation, if any
        # Agent hotkey: app + window title in front when the recording started
        # (only collected when the user lets them go along).
        self._agent_context = None
        self._send_lock = threading.Lock()
        self._mic_missing = None  # name of a chosen mic we last couldn't find
        self._migrate_mic_setting()

    def _migrate_mic_setting(self):
        """Up to 2.9 the mic was saved as a PortAudio index, and an index
        points at a different device as soon as one is plugged in or out.
        Keep the name of the device the index means right now instead."""
        index = self.config.get("mic_device_index")
        if index is None or self.config.get("mic_device_name"):
            return
        try:
            self.config["mic_device_name"] = input_device_name(int(index))
        except Exception:
            self.config["mic_device_name"] = None
        self.config["mic_device_index"] = None
        try:
            save_config(self.config)
        except Exception as e:
            logger.warning(f"Saving the migrated mic setting failed: {e}")

    def send(self, msg_type, data=None):
        """Send a JSON message to Electron via stdout. No-op if stdout is None
        (we were launched without a parent pipe)."""
        if sys.stdout is None:
            return
        msg = {"type": msg_type}
        if data:
            msg["data"] = data
        line = json.dumps(msg, ensure_ascii=False)
        try:
            # Several threads send (audio levels, preview, attachments).
            with self._send_lock:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
        except Exception:
            pass

    def load_model(self):
        """Load the configured Whisper model (blocks until that is settled)."""
        model_size = self.config.get("model_size", "small")
        self.send("status", {"message": f"Loading model '{model_size}'..."})
        logger.info(f"Loading model: {model_size}")
        self.engine.load(model_size, self.config.get("compute_type", "auto"))

    def _on_engine_ready(self, info: dict):
        """A model is loaded and passed its selftest (at the start, after a
        switch, or after the engine replaced a failed process)."""
        # A GPU accepted the compute type at load time and rejected it at
        # inference time: keep the one that works for the next app start.
        if info["compute_fallback"]:
            self.config["compute_type"] = info["compute_type"]
            save_config(self.config)
            logger.info(f"Persisted compute_type fallback: {info['compute_type']}")
        self.send("model_ready", {"device": info["device"], "model": info["model"]})

    def _amplitude_sender(self):
        """Send amplitude data to Electron while recording."""
        while self._running and self.recorder and self.recorder.is_recording:
            # Get amplitude from recorder's internal state
            time.sleep(0.04)  # ~25fps

    def _on_amplitude(self, amplitude, bands=None):
        """Called by AudioRecorder per block: level + spectrum for the overlay."""
        self.send("amplitude", {"value": round(amplitude, 4), "bands": bands or []})

    def handle_start_recording(self, command: bool = False, agent: bool = False):
        """Start audio recording. `command`: the command hotkey started it,
        `agent`: the agent hotkey (the text goes to a URL, not into the app)."""
        logger.info("Recording START" + (" (agent)" if agent else ""))
        self.corrections.cancel()
        if self.recorder and self.recorder.is_recording:
            self.recorder.stop()  # stray take – the rescan needs every stream closed
        self._drop_spool()
        self._cancel_requested = False
        # On disk from the first word on. A spoken command isn't worth keeping:
        # without its selection it means nothing later.
        if not command and self.config.get("rescue_audio", True):
            self._spool = audio_spool.start()
        if self._spool:
            self._spools_in_work.add(self._spool.id)
        try:
            self.recorder = self._open_recorder(self._spool)
        except Exception:
            self._drop_spool()
            raise
        self._record_start_time = time.time()
        self.send("recording_started")
        # After recording_started, so an Esc can never reach Electron first.
        self.esc_hook.arm()
        if self.config.get("mute_while_dictating", False):
            # Let the start chime finish before the speakers go quiet.
            self.muter.mute(delay=0.5 if self.config.get("overlay_sounds", True) else 0.0)
        self._end_context()  # a stray session (stop without a recorder)
        self._agent_context = None
        if agent:
            # Where the user is right now, read once. No clipboard, no screen.
            if self.config.get("agent_send_context", True):
                app, title = foreground_window()
                self._agent_context = {"app": app, "window_title": title}
        elif not command and self.config.get("ai_context", True):
            target = classify_foreground()
            if target:
                logger.info(f"AI context: {target.kind} ({target.app})")
                self._context = self.context.begin(target)

        # An unloaded cleanup LLM loads while the user speaks (~5 s) instead
        # of being skipped when the dictation is done.
        if command or self.config.get("cleanup_mode", "fast") == "ai":
            warm_up_async(self._llm_model())

        # Has a driver reset killed the GPU since the last dictation? Better to
        # find out now, while the user is talking, than after they finished.
        self.engine.probe()

        if self.config.get("live_preview_enabled", True):
            threading.Thread(target=self._preview_loop,
                             args=(self.recorder,), daemon=True).start()

    def _drop_spool(self):
        """The running recording isn't needed anymore (or never started)."""
        spool, self._spool = self._spool, None
        if spool:
            spool.discard()
            self._spools_in_work.discard(spool.id)

    def _open_recorder(self, spool=None):
        """A started recorder on the chosen mic, else on the system default.

        Rescans first (~35 ms): PortAudio only enumerates devices when it
        starts, so without it a headset plugged in later, or a new Windows
        default mic, would stay invisible until NoType restarts."""
        refresh_devices()
        name = self.config.get("mic_device_name")
        if name:
            index = find_input_device(name)
            if index is not None:
                rec = AudioRecorder(on_amplitude=self._on_amplitude, device_index=index,
                                    spool=spool)
                try:
                    rec.start()
                    self._mic_missing = None
                    return rec
                except Exception as e:
                    logger.warning(f"Opening the chosen mic failed: {e}")
            logger.warning("Chosen mic not available, recording from the system default")
            if self._mic_missing != name:  # tell once, not on every dictation
                self._mic_missing = name
                self.send("mic_fallback", {"name": name})
        rec = AudioRecorder(on_amplitude=self._on_amplitude, spool=spool)
        rec.start()
        return rec

    def _on_escape(self):
        """Esc during a dictation (runs on the hook thread, keep it short).
        The flag stops a dictation that is already being processed before
        anything gets inserted; Electron cancels a running recording."""
        self._cancel_requested = True
        self.send("escape_pressed")

    def handle_cancel_recording(self):
        """Drop the running recording: nothing is transcribed or inserted."""
        self._cancel_requested = True
        self.esc_hook.disarm()
        self.muter.restore()
        self._end_context()
        if self.recorder and self.recorder.is_recording:
            self.recorder.stop()
            logger.info("Recording CANCELLED")
        self._drop_spool()

    def _end_context(self):
        """Stop collecting copies; returns the session (None if there was none)."""
        session, self._context = self._context, None
        if session:
            self.context.close(session)
        return session

    def _cancelled(self, done: dict) -> bool:
        """Esc came in while processing: report and skip the rest."""
        if not self._cancel_requested:
            return False
        logger.info("Dictation cancelled before insertion")
        self.send("transcription_done", {"text": "", "empty": True, "cancelled": True, **done})
        return True

    def _preview_loop(self, recorder):
        """Live preview: while recording, transcribe a rolling window of the
        most recent audio (beam 1, cheap) and stream the tail to the overlay.

        Throwaway text – the final full-quality pass after stop is what gets
        inserted. The engine runs one job at a time, so a preview pass in
        flight can delay the final pass by at most one window (~0.3 s). While
        the engine is being replaced there is simply no caption for a moment."""
        # Same language policy as the final pass – otherwise the caption and
        # the inserted text visibly disagree for mixed speech.
        languages, allow_other = self._languages()
        opts = dict(languages=languages, allow_other=allow_other, beam_size="1")
        while self._running and recorder.is_recording:
            time.sleep(1.0)
            # Recorder may have been stopped/replaced while we slept.
            if not recorder.is_recording or recorder is not self.recorder:
                break
            # 5 s window: cheaper per pass than 8 s (the caption only shows the
            # tail anyway) and a shorter worst-case wait for the final pass.
            audio = recorder.get_recent_audio(5.0)
            if len(audio) < 16000:  # need at least 1s to say anything useful
                continue
            result = self.engine.preview(audio, opts)
            if result is None:
                continue
            if not recorder.is_recording or recorder is not self.recorder:
                break
            text = self._spoken_punctuation(result)
            text = dictionary.apply(text, self.config.get("dictionary"))
            tail = " ".join(text.split())[-120:]
            if tail:
                self.send("preview", {"text": _safe_str(tail)})

    # ── Hold-to-speak: precise key-up detection ──────────────────────
    # Electron's globalShortcut has no key-up event, so the UI used to stop a
    # hold-to-speak recording 350 ms after the LAST OS key-repeat. That added
    # 350 ms to every dictation – and if the key was held shorter than the OS
    # repeat delay (~500 ms) the timer fired before the first repeat and cut
    # the recording off mid-word. Here we poll the physical key state instead
    # (GetAsyncKeyState, every 15 ms) and report the real release.
    _VK = {
        "ctrl": 0x11, "control": 0x11, "cmdorctrl": 0x11, "commandorcontrol": 0x11,
        "shift": 0x10, "alt": 0x12, "altgr": 0x12, "menu": 0x12,
        "win": (0x5B, 0x5C), "meta": (0x5B, 0x5C), "super": (0x5B, 0x5C),
        "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
        "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E,
        "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
        "up": 0x26, "arrowup": 0x26, "down": 0x28, "arrowdown": 0x28,
        "left": 0x25, "arrowleft": 0x25, "right": 0x27, "arrowright": 0x27,
        "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91, "pause": 0x13,
        "printscreen": 0x2C, "contextmenu": 0x5D,
        # Named punctuation from the settings recorder → real characters,
        # resolved against the active keyboard layout below.
        "less": "<", "greater": ">", "plus": "+", "minus": "-",
        "comma": ",", "period": ".",
    }

    @staticmethod
    def _vk_for_char(ch: str):
        """Layout-aware VK for one printable character (ä, <, #, …)."""
        if not sys.platform.startswith("win"):
            return None
        r = ctypes.windll.user32.VkKeyScanW(ord(ch))
        if r == -1 or (r & 0xFFFF) == 0xFFFF:
            return None
        return r & 0xFF

    @classmethod
    def _hotkey_vks(cls, hotkey: str):
        """Map 'ctrl+shift+space' → virtual-key codes. [] if any token is
        unknown (then the UI keeps its timer fallback)."""
        vks = []
        for tok in (hotkey or "").lower().split("+"):
            tok = tok.strip()
            if not tok:
                continue
            if tok in cls._VK:
                v = cls._VK[tok]
                if isinstance(v, str):
                    v = cls._vk_for_char(v)
                    if v is None:
                        return []
                vks.append(v)
            elif len(tok) == 1 and tok.isascii() and tok.isalnum():
                vks.append(ord(tok.upper()))
            elif len(tok) == 1:
                v = cls._vk_for_char(tok)
                if v is None:
                    return []
                vks.append(v)
            elif tok[:1] == "f" and tok[1:].isdigit() and 1 <= int(tok[1:]) <= 24:
                vks.append(0x70 + int(tok[1:]) - 1)
            else:
                return []
        return vks

    def handle_watch_hotkey_release(self, hotkey: str):
        vks = self._hotkey_vks(hotkey)
        if not vks or not sys.platform.startswith("win"):
            self.send("hotkey_watch", {"ok": False})
            return
        self.send("hotkey_watch", {"ok": True})
        rec = self.recorder
        user32 = ctypes.windll.user32

        def down(v):
            if isinstance(v, tuple):
                return any(user32.GetAsyncKeyState(x) & 0x8000 for x in v)
            return bool(user32.GetAsyncKeyState(v) & 0x8000)

        def loop():
            deadline = time.monotonic() + 600  # never outlive a sane dictation
            while self._running and time.monotonic() < deadline:
                # Recording ended some other way (toggle, error, new session)?
                if rec is None or rec is not self.recorder or not rec.is_recording:
                    return
                if not any(down(v) for v in vks):
                    self.send("hotkey_released")
                    return
                time.sleep(0.015)

        threading.Thread(target=loop, daemon=True).start()

    def handle_stop_recording(self, command: bool = False, agent: bool = False,
                              version: str = ""):
        """Stop recording and transcribe. `command`: the recording is a spoken
        instruction for the selected text (command mode) instead of dictation.
        `agent`: the text goes to the agent URL instead of being typed
        (`version` is the app version for the User-Agent).

        The transcription runs on its own thread: it can take minutes on a
        processor, and this loop has to stay free for Esc and the watchdog."""
        if not self.recorder:
            self.esc_hook.disarm()
            self.send("error", {"message": "Not recording"})
            return

        logger.info("Recording STOP" + (" (command)" if command else " (agent)" if agent else ""))
        audio = self.recorder.stop()
        spool, self._spool = self._spool, None
        self.muter.restore()
        context = self._end_context()
        take = {
            "audio": audio, "context": context, "command": command, "agent": agent,
            "version": version,
            "seconds": time.time() - getattr(self, "_record_start_time", 0),
            "window": _foreground_hwnd(), "stopped_at": time.monotonic(),
        }
        threading.Thread(target=self._process_take, args=(take, spool), daemon=True).start()

    def _process_take(self, take: dict, spool):
        """Whatever happens in here, the audio is safe: its rescue copy only
        goes away once the dictation came out as text or was cancelled."""
        done = {"command": True} if take["command"] else {"agent": True} if take["agent"] else {}
        keep = False
        try:
            if spool:
                spool.close()
            self._stop_and_transcribe(take, done)
        except JobCancelled:
            self._cancelled(done)
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            keep = bool(spool) and spool.seconds >= audio_spool.MIN_SECONDS
            self.send("transcription_failed", {"saved": spool.id if keep else None, **done})
        finally:
            self.esc_hook.disarm()
            if spool:
                if not keep:
                    spool.discard()
                self._spools_in_work.discard(spool.id)
            if keep:
                self._send_rescue_list()

    def _stop_and_transcribe(self, take: dict, done: dict):
        audio, context = take["audio"], take["context"]
        command, agent = take["command"], take["agent"]

        if self._cancelled(done):
            return

        if len(audio) == 0:
            logger.warning("No audio captured")
            self.send("transcription_done", {"text": "", "empty": True, **done})
            return

        self.send("transcribing")

        # Check min recording length first (before expensive transcription)
        if take["seconds"] < 0.5:
            logger.info(f"Recording too short ({take['seconds']:.1f}s), discarding")
            self.send("transcription_done", {"text": "", "empty": True, "reason": "too_short", **done})
            return

        # Command mode: grab the selection right away, while the target
        # app still has focus and nothing has moved.
        selection = ""
        if command:
            with self.context.own_writes():
                selection = copy_selection()

        entries = self.config.get("dictionary") or []
        opts = self._transcribe_opts(entries)
        result = self.engine.transcribe(
            audio, opts,
            on_status=lambda stage, percent: self.send(
                "transcribe_status", {"stage": stage, "percent": percent}),
            should_abort=lambda: self._cancel_requested,
        )

        text = result["text"]
        stats = {
            "beam": result["beam"],
            "compute_type": result["compute_type"],
            "duration": result["duration"],
        }

        # The transcriber downgraded an unsupported compute type mid-call
        # (cuBLAS refusal) – persist the working one so the next app start
        # doesn't repeat the failed load+retry cycle.
        if result.get("compute_fallback"):
            self.config["compute_type"] = result["compute_type"]
            save_config(self.config)
            logger.info(f"Persisted compute_type fallback: {result['compute_type']}")

        if self._cancelled({**done, **stats}):
            return

        self.send("transcribe_status", {"stage": "cleanup"})

        if command:
            self._run_command(text, selection, entries, stats)
            return

        if text:
            text = self._polish(result, entries, stats)

        if self._cancelled({**done, **stats}):
            return
        self.esc_hook.disarm()  # from here on Esc belongs to the app again

        if agent:
            if text:
                self._send_to_agent(text, result.get("language") or opts["languages"][0][0],
                                    take["version"])
            else:
                logger.info("No speech detected")
                self.send("transcription_done", {"text": "", "empty": True, **done, **stats})
            return

        if text:
            logger.info(f"Transcribed: {len(text)} chars")
            if (time.monotonic() - take["stopped_at"] > FOCUS_GUARD_SECONDS
                    and _foreground_hwnd() != take["window"]):
                with self.context.own_writes():
                    copied = copy_text(text)
                logger.info(f"Window changed while transcribing, not pasting (clipboard: {copied})")
                self.send("transcription_done", {"text": text, "clipboard": True, **stats})
                return
            method = self.config.get("insert_method", "auto")
            items = self.context.finish(context) if context else []
            with self.context.own_writes():
                if items:
                    inserted = insert_with_context(text, items, context.target, method)
                else:
                    inserted = insert_text(text, method=method)
            if items:
                logger.info(f"Inserted with {len(items)} attachment(s) into {context.target.kind}")
                if inserted:
                    self.context.consume(items)
                stats["attachments"] = len(items)
            if inserted and self.config.get("learn_corrections", True):
                self.corrections.watch(
                    text, known={(e.get("from"), e.get("to")) for e in entries})
            self.send("transcription_done", {"text": text, **stats})
        else:
            logger.info("No speech detected")
            self.send("transcription_done", {"text": "", "empty": True, **stats})

    def _transcribe_opts(self, entries) -> dict:
        """Everything the final pass needs from the settings."""
        languages, allow_other = self._languages()
        return dict(
            languages=languages, allow_other=allow_other,
            beam_size=self.config.get("beam_size", "auto"),
            vad_sensitivity=self.config.get("vad_sensitivity", 300),
            # Learned spellings join the vocabulary as a hint for Whisper.
            initial_prompt=dictionary.whisper_prompt(self.config.get("initial_prompt", ""), entries),
        )

    def _polish(self, result: dict, entries, stats: dict) -> str:
        """Transcript → the text the user gets."""
        # "Fragezeichen", "neue Zeile" … → characters, first: the
        # dictionary and the LLM then see the finished punctuation.
        text = self._spoken_punctuation(result)
        # Dictionary before the cleanup (so the LLM already sees the
        # right words) and after it (the LLM may re-split or
        # re-capitalize a term). Idempotent, ~1 ms per pass.
        text = dictionary.apply(text, entries)
        cleaned = clean_transcript(
            text,
            mode=self.config.get("cleanup_mode", "fast"),
            llm_model=self.config.get("llm_model", DEFAULT_LLM_MODEL),
            llm_min_words=self.config.get("llm_min_words", 8),
            terms=dictionary.hint_terms(self.config.get("initial_prompt", ""), entries),
        )
        stats["cleanup"] = cleaned["engine"]
        stats["cleanup_ms"] = cleaned["ms"]
        return dictionary.apply(cleaned["text"] or text, entries)

    # ── Saved recordings ─────────────────────────────────────────────
    def _send_rescue_list(self, startup: bool = False):
        self.send("rescue_available", {
            "items": audio_spool.list_items(self._spools_in_work),
            "dir": audio_spool.RESCUE_DIR,
            "startup": startup,
        })

    def handle_rescue_run(self, item_id: str):
        """Transcribe a saved recording again. It ends on the clipboard and in
        the history, never in whatever window happens to be in front."""
        if not self._rescue_lock.acquire(blocking=False):
            return  # one at a time
        threading.Thread(target=self._rescue, args=(item_id,), daemon=True).start()

    def _rescue(self, item_id: str):
        try:
            audio = audio_spool.load(item_id)
            logger.info(f"Transcribing a saved recording ({len(audio) / 16000:.0f}s)")
            entries = self.config.get("dictionary") or []
            result = self.engine.transcribe(audio, self._transcribe_opts(entries))
            text = self._polish(result, entries, {}) if result["text"] else ""
            if text:
                with self.context.own_writes():
                    copy_text(text)
            audio_spool.delete(item_id)
            logger.info(f"Saved recording transcribed: {len(text)} chars")
            self.send("rescue_done", {"id": item_id, "text": _safe_str(text)})
        except Exception as e:
            logger.error(f"Saved recording failed: {e}", exc_info=True)
            self.send("rescue_failed", {"id": item_id})
        finally:
            self._rescue_lock.release()
            self._send_rescue_list()

    def handle_rescue_delete(self, item_id=None):
        """One saved recording, or all of them."""
        if item_id:
            audio_spool.delete(item_id)
        else:
            audio_spool.clear(self._spools_in_work)
        self._send_rescue_list()

    def _languages(self):
        """The languages the user speaks as [(code, share)] plus whether
        other languages are accepted, too. A config from before the language
        list (one language + "detect automatically") reads as that language
        at 100 %."""
        main = self.config.get("language") or "de"
        languages = normalize_languages(self.config.get("languages") or [main], fallback=main)
        return languages, self.config.get("auto_language_detect", True)

    def _spoken_punctuation(self, result: dict) -> str:
        """Transcript with spoken punctuation turned into characters (if on)."""
        if not self.config.get("spoken_punctuation", True):
            return result["text"]
        return spoken_punctuation.apply(result["text"], result.get("language"))

    def _send_to_agent(self, text: str, language: str, version: str):
        """Agent hotkey: POST the dictation to the configured URL and report
        the answer (agent_result). The request runs on its own thread, so the
        stdin loop (hotkeys, Esc, the next recording) never waits on it."""
        url = self.config.get("agent_url") or ""
        token = self.config.get("agent_token") or ""
        context = self._agent_context if self.config.get("agent_send_context", True) else None
        payload = agent_webhook.build_payload(text, language=language, context=context)
        logger.info(f"Agent: sending {len(text)} chars"
                    + (" with context" if context is not None else ""))
        self.send("agent_sending", {"text": _safe_str(text)})

        def worker():
            result = agent_webhook.post(url, payload, token=token, version=version)
            if result.get("message"):
                result["message"] = _safe_str(result["message"])
            self.send("agent_result", result)

        threading.Thread(target=worker, daemon=True).start()

    def handle_agent_test(self, data: dict):
        """Settings "Send test" button: a fixed test body to the URL and token
        typed in the form (saved or not). Answers with agent_result + test."""
        url = str(data.get("url") or "")
        token = str(data.get("token") or "")
        version = str(data.get("version") or "")

        def worker():
            result = agent_webhook.post(url, agent_webhook.build_test_payload(),
                                        token=token, version=version)
            if result.get("message"):
                result["message"] = _safe_str(result["message"])
            self.send("agent_result", {**result, "test": True})

        threading.Thread(target=worker, daemon=True).start()

    def _run_command(self, spoken: str, selection: str, entries, stats: dict):
        """Command mode: rewrite the selection per the spoken instruction (or
        write new text at the cursor) with the local LLM, then paste."""
        instruction = dictionary.apply(rule_clean(spoken or ""), entries)
        if not instruction:
            self.send("transcription_done", {"text": "", "empty": True, "command": True, **stats})
            return
        # AI cleanup may be off – command mode still needs the LLM, so bring
        # Ollama up on demand (no-op when it's already running).
        if ollama_manager.ensure_running_if_installed():
            reset_availability_cache()
        started = time.perf_counter()
        result = run_command(instruction, selection, model=self._llm_model())
        ms = int((time.perf_counter() - started) * 1000)
        logger.info(f"Command ({len(instruction)} chars) on {len(selection)} chars "
                    f"-> {len(result or '')} chars ({ms} ms)")
        if not result:
            installed = ollama_manager.status(self._llm_model())
            reason = ("command_no_llm" if not (installed.get("installed") and installed.get("model_pulled"))
                      else "command_failed")
            self.send("transcription_done", {"text": "", "empty": True, "command": True,
                                             "reason": reason, **stats})
            return
        if self._cancelled({"command": True, **stats}):
            return
        self.esc_hook.disarm()
        with self.context.own_writes():
            insert_text(result, method=self.config.get("insert_method", "auto"))
        self.send("transcription_done", {"text": result, "command": True,
                                         "instruction": instruction,
                                         "replaced": bool(selection.strip()),
                                         "llm_ms": ms, **stats})

    def _llm_model(self) -> str:
        return self.config.get("llm_model", DEFAULT_LLM_MODEL)

    def _ensure_ai_ready(self):
        """Bring Ollama up (if installed) and preload the cleanup LLM.
        Runs in a background thread – never blocks the dictation path."""
        if ollama_manager.ensure_running_if_installed():
            reset_availability_cache()
            warm_up(self._llm_model())

    def handle_ollama_status(self):
        self.send("ollama_status", ollama_manager.status(self._llm_model()))

    def handle_ollama_setup(self):
        """Install portable Ollama + pull the LLM, streaming progress to the
        UI. Runs in a background thread; finishes by enabling AI cleanup."""
        def progress(stage, percent, message):
            self.send("ollama_progress",
                      {"stage": stage, "percent": percent, "message": message})

        def worker():
            ok = ollama_manager.setup(self._llm_model(), on_progress=progress)
            if ok:
                reset_availability_cache()
                warm_up(self._llm_model())
                # Setup was an explicit user action – switch cleanup to AI.
                self.config["cleanup_mode"] = "ai"
                save_config(self.config)
                self.send("config", self.config)
            # "final" marks end-of-setup so the UI can unlock its button –
            # routine status refreshes while setup runs are ignored there.
            self.send("ollama_status",
                      {**ollama_manager.status(self._llm_model()), "final": True})

        threading.Thread(target=worker, daemon=True).start()

    def handle_ollama_update(self):
        """Update the portable Ollama in place. Only ever user-triggered –
        the release check on GitHub is the click's sole network access."""
        def progress(stage, percent, message):
            self.send("ollama_progress",
                      {"stage": stage, "percent": percent, "message": message})

        def worker():
            ok, note = ollama_manager.update(on_progress=progress)
            reset_availability_cache()
            if self.config.get("cleanup_mode") == "ai":
                self._ensure_ai_ready()
            self.send("ollama_status",
                      {**ollama_manager.status(self._llm_model()),
                       "final": True, "note": note, "ok": ok})

        threading.Thread(target=worker, daemon=True).start()

    def handle_save_config(self, new_config):
        """Save config and reload if needed."""
        old_model = self.config.get("model_size")
        old_cleanup = self.config.get("cleanup_mode", "fast")
        old_llm = self._llm_model()
        old_rescue = self.config.get("rescue_audio", True)
        self.config.update(new_config)
        save_config(self.config)

        # Different cleanup LLM while AI mode is on → preload it (if pulled)
        # so the next dictation doesn't pay the cold start; the old model's
        # availability verdict is stale now.
        if self._llm_model() != old_llm:
            reset_availability_cache()
            if self.config.get("cleanup_mode", "fast") == "ai":
                threading.Thread(target=self._ensure_ai_ready, daemon=True).start()

        # Reload model if size changed
        if new_config.get("model_size") and new_config["model_size"] != old_model:
            threading.Thread(target=self.load_model, daemon=True).start()

        # No more copies on disk means none of the old ones either.
        if old_rescue and not self.config.get("rescue_audio", True):
            self.handle_rescue_delete()

        # AI mode switched on → bring Ollama up and preload the LLM.
        # Switched off → stop our portable server to free RAM/VRAM ("pause").
        new_cleanup = self.config.get("cleanup_mode", "fast")
        if new_cleanup == "ai" and old_cleanup != "ai":
            threading.Thread(target=self._ensure_ai_ready, daemon=True).start()
        elif new_cleanup != "ai" and old_cleanup == "ai":
            threading.Thread(target=ollama_manager.stop, daemon=True).start()
            reset_availability_cache()

        self.send("config_saved", self.config)

    def handle_get_config(self):
        """Send current config to Electron."""
        self.send("config", self.config)

    # ── Model catalog for the UI (model cards) ───────────────────────
    _gpu_info_cache = None

    @classmethod
    def _gpu_info(cls):
        """Name + VRAM of the primary NVIDIA GPU via nvidia-smi. Cached: the
        query costs ~100 ms and the answer never changes while we run."""
        if cls._gpu_info_cache is not None:
            return cls._gpu_info_cache or None
        info = None
        candidates = ["nvidia-smi"]
        if sys.platform.startswith("win"):
            candidates += [
                os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe"),
                r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            ]
        for exe in candidates:
            try:
                import subprocess
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                out = subprocess.run(
                    [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=4, creationflags=flags,
                ).stdout.strip().splitlines()
                if out and "," in out[0]:
                    name, mem = out[0].rsplit(",", 1)
                    info = {"name": _safe_str(name.strip()), "vram_mb": int(float(mem.strip()))}
                    break
            except Exception:
                continue
        cls._gpu_info_cache = info or False
        return info

    def handle_list_models(self):
        """Catalog + per-model 'already downloaded' state (from the Hugging
        Face cache) + hardware, so the UI can badge cards as 'offline ready'
        and recommend a model that fits the GPU."""
        hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
            os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"),
            "hub",
        )
        models = []
        for mid, entry in MODEL_CATALOG.items():
            repo = entry["repo"]
            if "/" not in repo:
                repo = BUILTIN_REPOS.get(repo, repo)
            folder = os.path.join(hub, "models--" + repo.replace("/", "--"))
            downloaded, size_mb = False, None
            try:
                snaps = os.path.join(folder, "snapshots")
                if os.path.isdir(snaps):
                    downloaded = any(
                        os.path.isfile(os.path.join(snaps, s, "model.bin"))
                        for s in os.listdir(snaps)
                    )
                if downloaded:
                    blobs = os.path.join(folder, "blobs")
                    total = 0
                    for f in os.listdir(blobs):
                        p = os.path.join(blobs, f)
                        if os.path.isfile(p) and not f.endswith(".incomplete"):
                            total += os.path.getsize(p)
                    size_mb = int(total / (1024 * 1024))
            except Exception:
                pass
            models.append({"id": mid, "downloaded": downloaded, "size_mb": size_mb})

        # Before the first load the engine can't know yet.
        device = self.engine.device or ("cuda" if self._gpu_info() else "cpu")
        self.send("models", {"device": device, "gpu": self._gpu_info(), "models": models})

    def run(self):
        """Main loop: read JSON commands from stdin."""
        logger.info("Backend starting...")
        # If we were launched without a parent that piped stdio, there is no
        # one to talk to – exit cleanly instead of busy-waiting forever.
        if sys.stdin is None or sys.stdout is None:
            logger.error("No stdio available (not launched as a child of Electron). Exiting.")
            return
        self.send("backend_ready")

        # A recording the last run didn't get to finish, and old ones out.
        audio_spool.purge()
        self._send_rescue_list(startup=True)

        # Load model in background
        threading.Thread(target=self.load_model, daemon=True).start()

        # AI cleanup enabled → start Ollama (if installed) and preload the
        # LLM in the background so the first dictation pays no cold-start.
        if self.config.get("cleanup_mode", "fast") == "ai":
            threading.Thread(target=self._ensure_ai_ready, daemon=True).start()

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
                cmd = msg.get("command")

                if cmd == "start_recording":
                    data = msg.get("data") or {}
                    self.handle_start_recording(command=bool(data.get("command")),
                                                agent=bool(data.get("agent")))
                elif cmd == "stop_recording":
                    self.handle_stop_recording()
                elif cmd == "stop_command":
                    self.handle_stop_recording(command=True)
                elif cmd == "stop_agent":
                    self.handle_stop_recording(
                        agent=True, version=str((msg.get("data") or {}).get("version") or ""))
                elif cmd == "agent_test":
                    self.handle_agent_test(msg.get("data") or {})
                elif cmd == "rescue_run":
                    self.handle_rescue_run((msg.get("data") or {}).get("id"))
                elif cmd == "rescue_delete":
                    self.handle_rescue_delete((msg.get("data") or {}).get("id"))
                elif cmd == "cancel_recording":
                    self.handle_cancel_recording()
                elif cmd == "pause_recording":
                    if self.recorder and self.recorder.is_recording:
                        self.recorder.pause()
                        self.send("recording_paused")
                elif cmd == "resume_recording":
                    if self.recorder:
                        self.recorder.resume()
                        self.send("recording_resumed")
                elif cmd == "list_devices":
                    try:
                        # Rescan so a mic plugged in since the start shows up
                        # (never while a stream is open).
                        if not (self.recorder and self.recorder.is_recording):
                            refresh_devices()
                        self.send("devices", {"devices": input_devices()})
                    except Exception as e:
                        self.send("error", {"message": f"Device listing failed: {e}"})
                elif cmd == "save_config":
                    self.handle_save_config(msg.get("data", {}))
                elif cmd == "get_config":
                    self.handle_get_config()
                elif cmd == "ollama_status":
                    self.handle_ollama_status()
                elif cmd == "ollama_setup":
                    self.handle_ollama_setup()
                elif cmd == "ollama_update":
                    self.handle_ollama_update()
                elif cmd == "list_models":
                    self.handle_list_models()
                elif cmd == "watch_hotkey_release":
                    self.handle_watch_hotkey_release((msg.get("data") or {}).get("hotkey", ""))
                elif cmd == "ping":
                    self.send("pong")
                elif cmd == "quit":
                    logger.info("Quit command received")
                    self._running = False
                    self.esc_hook.disarm()
                    self.muter.shutdown()
                    ollama_manager.stop()  # don't orphan our serve child
                    self.engine.stop()
                    break
                else:
                    logger.warning(f"Unknown command: {cmd}")

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
            except Exception as e:
                logger.error(f"Command error: {e}", exc_info=True)
                self.send("error", {"message": str(e)})

        # Also reached when Electron dies and stdin closes – never orphan
        # the portable ollama serve child, never leave the speakers muted.
        self.esc_hook.disarm()
        self.muter.shutdown()
        ollama_manager.stop()
        self.engine.stop()
        logger.info("Backend stopped.")


if __name__ == "__main__":
    backend = Backend()
    backend.run()
