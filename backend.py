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
import numpy as np

# Ensure our directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_recorder import AudioRecorder
from transcriber import Transcriber
from text_output import insert_text
from config import load_config, save_config, get_config, CONFIG_DIR
from postprocess import (clean_transcript, warm_up, reset_availability_cache,
                         DEFAULT_LLM_MODEL)
import ollama_manager

# Log to file only (stdout is for IPC). Rotate so the log file never grows unbounded.
# CONFIG_DIR is %APPDATA%/NoType — same directory the frontend uses, survives rebuilds.
LOG_FILE = os.path.join(CONFIG_DIR, "notype.log")
_log_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
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

    def _amplitude_sender(self):
        """Send amplitude data to Electron while recording."""
        while self._running and self.recorder and self.recorder.is_recording:
            # Get amplitude from recorder's internal state
            time.sleep(0.04)  # ~25fps

    def _on_amplitude(self, amplitude):
        """Called by AudioRecorder with amplitude data."""
        self.send("amplitude", {"value": round(amplitude, 4)})

    def handle_start_recording(self):
        """Start audio recording."""
        logger.info("Recording START")
        device_index = self.config.get("mic_device_index", None)
        self.recorder = AudioRecorder(
            on_amplitude=self._on_amplitude,
            device_index=device_index,
        )
        self.recorder.start()
        self._record_start_time = time.time()
        self.send("recording_started")

    def handle_stop_recording(self):
        """Stop recording and transcribe."""
        if not self.recorder:
            self.send("error", {"message": "Not recording"})
            return

        logger.info("Recording STOP")
        audio = self.recorder.stop()

        if len(audio) == 0:
            logger.warning("No audio captured")
            self.send("transcription_done", {"text": "", "empty": True})
            return

        self.send("transcribing")

        try:
            # Check min recording length first (before expensive transcription)
            duration = time.time() - getattr(self, '_record_start_time', 0)
            if duration < 0.5:
                logger.info(f"Recording too short ({duration:.1f}s), discarding")
                self.send("transcription_done", {"text": "", "empty": True, "reason": "too_short"})
                return

            language = self.config.get("language", "de")
            auto_detect = self.config.get("auto_language_detect", True)
            beam_size = self.config.get("beam_size", "auto")
            vad_sensitivity = self.config.get("vad_sensitivity", 300)
            initial_prompt = self.config.get("initial_prompt", "") or None

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

            if text:
                cleaned = clean_transcript(
                    text,
                    mode=self.config.get("cleanup_mode", "fast"),
                    llm_model=self.config.get("llm_model", DEFAULT_LLM_MODEL),
                )
                text = cleaned["text"] or text
                stats["cleanup"] = cleaned["engine"]
                stats["cleanup_ms"] = cleaned["ms"]

            if text:
                logger.info(f"Transcribed: '{_safe_str(text[:80])}'")
                time.sleep(0.15)
                insert_text(text)
                self.send("transcription_done", {"text": text, **stats})
            else:
                logger.info("No speech detected")
                self.send("transcription_done", {"text": "", "empty": True, **stats})

        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            self.send("error", {"message": f"Transcription failed: {e}"})

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

    def handle_save_config(self, new_config):
        """Save config and reload if needed."""
        old_model = self.config.get("model_size")
        old_cleanup = self.config.get("cleanup_mode", "fast")
        self.config.update(new_config)
        save_config(self.config)

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
