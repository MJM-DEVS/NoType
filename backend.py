"""
NoType – Python Backend (IPC via stdin/stdout)
Receives JSON commands from Electron, sends JSON responses.
Handles audio recording, transcription, and text insertion.
"""

import sys
import os
import io

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
import numpy as np

# Ensure our directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_recorder import AudioRecorder
from transcriber import Transcriber
from text_output import insert_text, copy_selection
from config import load_config, save_config, CONFIG_DIR
from postprocess import (clean_transcript, warm_up, reset_availability_cache,
                         rule_clean, run_command, DEFAULT_LLM_MODEL)
import dictionary
import ollama_manager
from correction_watch import CorrectionWatcher

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
        self.transcriber = None
        self._amplitude_thread = None
        self._running = True
        # Dictionary stage 2: fixes the user types right after a dictation
        # are offered as dictionary entries (Electron asks first).
        self.corrections = CorrectionWatcher(
            lambda items: self.send("dictionary_suggestions", {
                "items": [{"from": heard, "to": meant} for heard, meant in items]}))

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
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    def init_transcriber(self):
        """Load Whisper model."""
        model_size = self.config.get("model_size", "small")
        self.send("status", {"message": f"Loading model '{model_size}'..."})
        logger.info(f"Loading model: {model_size}")

        self.transcriber = Transcriber(
            model_size=model_size,
            compute_type=self.config.get("compute_type", "auto"),
        )
        try:
            self.transcriber.load_model(
                on_progress=lambda msg: self.send("status", {"message": msg})
            )
            self._inference_selftest()
            device_info = self.transcriber.get_device_info()
            self.send("model_ready", {"device": device_info, "model": model_size})
            logger.info(f"Model loaded on {device_info}")
        except Exception as e:
            logger.error(f"Model load failed: {e}")
            self.send("error", {"message": f"Model load failed: {e}"})
            # Retry on CPU
            try:
                self.transcriber = Transcriber(model_size=model_size, device="cpu")
                self.transcriber.load_model()
                self.send("model_ready", {"device": "CPU (int8)", "model": model_size})
            except Exception as e2:
                self.send("error", {"message": f"CPU fallback failed: {e2}"})

    def _inference_selftest(self):
        """Push half a second of audio through the freshly loaded model with
        VAD off, so the GPU GEMM path actually runs. Some GPUs accept a
        compute type at LOAD time but reject it at INFERENCE time (cuBLAS
        NOT_SUPPORTED) – trigger that here, where transcribe()'s float16
        fallback fixes it invisibly, instead of on the user's first dictation.
        Runs inside the model-loading background thread."""
        try:
            silence = np.zeros(8000, dtype=np.float32)  # 0.5s @ 16kHz
            result = self.transcriber.transcribe(
                silence, language="de", auto_detect=False,
                beam_size="1", vad_filter=False,
            )
            if result.get("compute_fallback"):
                self.config["compute_type"] = result["compute_type"]
                save_config(self.config)
                logger.info(f"Selftest downgraded compute_type to "
                            f"{result['compute_type']} (persisted)")
            else:
                logger.info("Inference selftest OK")
        except Exception as e:
            # Selftest must never block startup – a real inference problem
            # will surface (and be handled) on the first dictation anyway.
            logger.warning(f"Inference selftest failed (non-fatal): {e}")

    def _amplitude_sender(self):
        """Send amplitude data to Electron while recording."""
        while self._running and self.recorder and self.recorder.is_recording:
            # Get amplitude from recorder's internal state
            time.sleep(0.04)  # ~25fps

    def _on_amplitude(self, amplitude, bands=None):
        """Called by AudioRecorder per block: level + spectrum for the overlay."""
        self.send("amplitude", {"value": round(amplitude, 4), "bands": bands or []})

    def handle_start_recording(self):
        """Start audio recording."""
        logger.info("Recording START")
        self.corrections.cancel()
        device_index = self.config.get("mic_device_index", None)
        self.recorder = AudioRecorder(
            on_amplitude=self._on_amplitude,
            device_index=device_index,
        )
        self.recorder.start()
        self._record_start_time = time.time()
        self.send("recording_started")

        if (self.config.get("live_preview_enabled", True)
                and self.transcriber and self.transcriber.is_loaded):
            threading.Thread(target=self._preview_loop,
                             args=(self.recorder,), daemon=True).start()

    def _preview_loop(self, recorder):
        """Live preview: while recording, transcribe a rolling window of the
        most recent audio (beam 1, cheap) and stream the tail to the overlay.

        Throwaway text – the final full-quality pass after stop is what gets
        inserted. Uses the shared inference lock, so a preview pass in flight
        can delay the final pass by at most one window (~0.3 s)."""
        language = self.config.get("language", "de")
        # Same language policy as the final pass – otherwise the caption is
        # forced into the configured language while the inserted text is
        # auto-detected, and the two visibly disagree for mixed speech.
        auto_detect = self.config.get("auto_language_detect", True)
        while self._running and recorder.is_recording:
            time.sleep(1.0)
            # Recorder may have been stopped/replaced while we slept.
            if not recorder.is_recording or recorder is not self.recorder:
                break
            # 5 s window: cheaper per pass than 8 s (the caption only shows the
            # tail anyway) and a shorter worst-case wait for the final pass,
            # which shares the inference lock with an in-flight preview.
            audio = recorder.get_recent_audio(5.0)
            if len(audio) < 16000:  # need at least 1s to say anything useful
                continue
            try:
                result = self.transcriber.transcribe(
                    audio, language=language, auto_detect=auto_detect,
                    beam_size="1",
                )
            except Exception as e:
                logger.warning(f"Preview pass failed, stopping preview: {e}")
                break
            if not recorder.is_recording or recorder is not self.recorder:
                break
            text = dictionary.apply(result["text"], self.config.get("dictionary"))
            tail = text[-120:]
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

    def handle_stop_recording(self, command: bool = False):
        """Stop recording and transcribe. `command`: the recording is a spoken
        instruction for the selected text (command mode) instead of dictation."""
        if not self.recorder:
            self.send("error", {"message": "Not recording"})
            return

        logger.info("Recording STOP" + (" (command)" if command else ""))
        audio = self.recorder.stop()
        done = {"command": True} if command else {}

        if len(audio) == 0:
            logger.warning("No audio captured")
            self.send("transcription_done", {"text": "", "empty": True, **done})
            return

        self.send("transcribing")

        try:
            # Check min recording length first (before expensive transcription)
            duration = time.time() - getattr(self, '_record_start_time', 0)
            if duration < 0.5:
                logger.info(f"Recording too short ({duration:.1f}s), discarding")
                self.send("transcription_done", {"text": "", "empty": True, "reason": "too_short", **done})
                return

            # Command mode: grab the selection right away, while the target
            # app still has focus and nothing has moved.
            selection = copy_selection() if command else ""

            language = self.config.get("language", "de")
            auto_detect = self.config.get("auto_language_detect", True)
            beam_size = self.config.get("beam_size", "auto")
            vad_sensitivity = self.config.get("vad_sensitivity", 300)
            entries = self.config.get("dictionary") or []
            # Learned spellings join the vocabulary as a hint for Whisper.
            initial_prompt = dictionary.whisper_prompt(
                self.config.get("initial_prompt", ""), entries)

            if not self.transcriber or not self.transcriber.is_loaded:
                self.send("error", {"message": "Model not loaded yet"})
                return

            result = self.transcriber.transcribe(
                audio, language=language, auto_detect=auto_detect,
                beam_size=beam_size, vad_sensitivity=vad_sensitivity,
                initial_prompt=initial_prompt,
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

            if command:
                self._run_command(text, selection, entries, stats)
                return

            if text:
                # Dictionary before the cleanup (so the LLM already sees the
                # right words) and after it (the LLM may re-split or
                # re-capitalize a term). Idempotent, ~1 ms per pass.
                text = dictionary.apply(text, entries)
                cleaned = clean_transcript(
                    text,
                    mode=self.config.get("cleanup_mode", "fast"),
                    llm_model=self.config.get("llm_model", DEFAULT_LLM_MODEL),
                    llm_min_words=self.config.get("llm_min_words", 8),
                    terms=dictionary.hint_terms(
                        self.config.get("initial_prompt", ""), entries),
                )
                text = dictionary.apply(cleaned["text"] or text, entries)
                stats["cleanup"] = cleaned["engine"]
                stats["cleanup_ms"] = cleaned["ms"]

            if text:
                logger.info(f"Transcribed: {len(text)} chars")
                if insert_text(text, method=self.config.get("insert_method", "auto")) \
                        and self.config.get("learn_corrections", True):
                    self.corrections.watch(
                        text, known={(e.get("from"), e.get("to")) for e in entries})
                self.send("transcription_done", {"text": text, **stats})
            else:
                logger.info("No speech detected")
                self.send("transcription_done", {"text": "", "empty": True, **stats})

        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            self.send("error", {"message": f"Transcription failed: {e}"})

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
            threading.Thread(target=self.init_transcriber, daemon=True).start()

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
        from transcriber import MODEL_CATALOG
        try:
            from faster_whisper.utils import _MODELS as fw_repos
        except Exception:
            fw_repos = {}
        hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
            os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"),
            "hub",
        )
        models = []
        for mid, entry in MODEL_CATALOG.items():
            repo = entry["repo"]
            if "/" not in repo:
                repo = fw_repos.get(repo, repo)
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

        device = "cpu"
        if self.transcriber is not None:
            device = self.transcriber.device
        else:
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    device = "cuda"
            except Exception:
                pass
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

        # Load model in background
        threading.Thread(target=self.init_transcriber, daemon=True).start()

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
                    self.handle_start_recording()
                elif cmd == "stop_recording":
                    self.handle_stop_recording()
                elif cmd == "stop_command":
                    self.handle_stop_recording(command=True)
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
                        import sounddevice as sd
                        devices = sd.query_devices()
                        input_devices = []
                        seen_names = set()
                        default_input = sd.default.device[0]
                        for i, d in enumerate(devices):
                            if d['max_input_channels'] > 0:
                                # Sanitize device name (fix encoding issues)
                                name = d['name']
                                try:
                                    name = name.encode('utf-8', errors='replace').decode('utf-8')
                                except Exception:
                                    name = name.encode('ascii', errors='replace').decode('ascii')
                                # Deduplicate by name
                                if name in seen_names:
                                    continue
                                seen_names.add(name)
                                input_devices.append({
                                    "index": i,
                                    "name": name,
                                    "channels": d['max_input_channels'],
                                    "default": i == default_input,
                                })
                        self.send("devices", {"devices": input_devices})
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
                    ollama_manager.stop()  # don't orphan our serve child
                    break
                else:
                    logger.warning(f"Unknown command: {cmd}")

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
            except Exception as e:
                logger.error(f"Command error: {e}", exc_info=True)
                self.send("error", {"message": str(e)})

        # Also reached when Electron dies and stdin closes – never orphan
        # the portable ollama serve child.
        ollama_manager.stop()
        logger.info("Backend stopped.")


if __name__ == "__main__":
    backend = Backend()
    backend.run()
