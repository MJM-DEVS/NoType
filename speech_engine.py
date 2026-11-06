"""
NoType – Speech engine, the backend's side.

Owns the process that runs Whisper (transcribe_worker.py) and makes its
failures disappear. The backend asks for text; whether that took a new
process, a retry or a move to the processor is handled in here.

What this is built around: when the GPU driver resets, the CUDA context of
every running process is dead and stays dead, while a new process works right
away. So a failed inference replaces the process and tries again:

    1st try   the running process
    2nd try   a new process, on the GPU if it comes up there
    3rd try   a new process on the processor

Only after that the dictation counts as failed, and its audio is still safe on
disk (audio_spool.py). How long a transcription takes is never a failure in
itself: only a process that stopped making progress is replaced.
"""

import itertools
import logging
import os
import queue
import subprocess
import sys
import threading
import time

from transcribe_worker import read_frame, write_frame
from transcriber import JobCancelled

logger = logging.getLogger("NoType.Engine")

# Seconds without any sign of progress before a process counts as stuck. A
# single window can take minutes on a slow processor, on a GPU it can't.
FINAL_LIMIT = {"cuda": 120, "cpu": 900}
PREVIEW_LIMIT = {"cuda": 30, "cpu": 180}
LOAD_LIMIT = 120
DOWNLOAD_LIMIT = 3600        # first use: the model comes from the internet
ATTEMPTS = 3
STATUS_AFTER = 3.0           # short dictations stay quiet in the overlay
GPU_RECHECK_DELAYS = (60, 120, 240, 480, 960)  # then not before the next start


class EngineError(Exception):
    """This audio could not be transcribed."""


def _worker_command() -> list:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--transcribe-worker"]
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend.py")
    return [sys.executable, script, "--transcribe-worker"]


class _Worker:
    """One child process with one loaded model."""

    def __init__(self, model_size: str, device: str, compute_type: str, on_status=None):
        self.model_size = model_size
        self.device = None if device == "auto" else device
        self.compute_type = compute_type
        self.compute_fallback = False
        self._on_status = on_status
        self._inbox = queue.SimpleQueue()
        self._send_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._job_ids = itertools.count(1)
        self._dead = False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(_worker_command(), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=flags)
        self._bind_lifetime()
        threading.Thread(target=self._read_messages, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._send({"t": "init", "model_size": model_size, "device": device,
                    "compute_type": compute_type})

    def _bind_lifetime(self):
        """The process dies with the backend, however the backend ends, so it
        can't keep GPU memory or files of an update locked."""
        if sys.platform.startswith("win"):
            import ollama_manager
            ollama_manager.bind_to_job(self.proc, "Speech engine")

    def _send(self, obj):
        try:
            with self._send_lock:
                write_frame(self.proc.stdin, obj)
        except (OSError, ValueError) as e:
            raise EngineError(f"Speech engine is gone: {e}")

    def _send_soon(self, obj):
        """For jobs and cancels: minutes of audio don't fit into the pipe at
        once, and a stuck process must not stuck the sender, too. A process
        that is gone shows up in the read loop."""
        def send():
            try:
                self._send(obj)
            except EngineError:
                pass

        threading.Thread(target=send, daemon=True).start()

    def _read_messages(self):
        while True:
            try:
                msg = read_frame(self.proc.stdout)
            except Exception as e:
                logger.warning(f"Bad message from the speech engine: {e}")
                msg = None
            if msg is None:
                self._dead = True
                self._inbox.put(None)
                return
            kind = msg.get("t")
            if kind == "log":
                logging.getLogger(msg["name"]).log(msg["level"], msg["msg"])
            elif kind == "status":
                if self._on_status:
                    self._on_status(msg["msg"])
            else:
                self._inbox.put(msg)

    def _read_stderr(self):
        # Native crash reports. Only ever engine internals, never text.
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                logger.warning(f"Speech engine: {line[:500]}")

    def _exit_note(self) -> str:
        try:
            code = self.proc.wait(timeout=1)
            return f"exit code {code}"
        except subprocess.TimeoutExpired:
            return "still running"

    def wait_ready(self):
        limit, last_sign = LOAD_LIMIT, time.monotonic()
        while True:
            try:
                msg = self._inbox.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() - last_sign > limit:
                    self.kill()
                    raise EngineError("Loading the model got stuck")
                continue
            if msg is None:
                raise EngineError(f"Speech engine ended while loading ({self._exit_note()})")
            kind = msg["t"]
            if kind == "loading":
                self.device = msg["device"]
                self.compute_type = msg["compute_type"]
                limit = DOWNLOAD_LIMIT if msg["download"] else LOAD_LIMIT
                last_sign = time.monotonic()
            elif kind == "load_failed":
                raise EngineError(msg["error"])
            elif kind == "ready":
                self.device = msg["device"]
                self.compute_type = msg["compute_type"]
                self.compute_fallback = msg["compute_fallback"]
                return

    @property
    def busy(self) -> bool:
        return self._job_lock.locked()

    def run(self, audio, opts, limit: float, on_tick=None, should_abort=None) -> dict:
        """Send one job and wait for its result. `on_tick(seconds_done)` comes
        about once a second while waiting."""
        while not self._job_lock.acquire(timeout=0.2):  # a preview pass in flight
            if should_abort and should_abort():
                raise JobCancelled()
            if on_tick:
                on_tick(0.0)
        try:
            if self._dead:
                raise EngineError("Speech engine is gone")
            job_id = next(self._job_ids)
            self._send_soon({"t": "job", "id": job_id, "audio": audio, "opts": opts,
                             "probe": audio is None})
            last_sign = last_tick = time.monotonic()
            done = 0.0
            while True:
                try:
                    msg = self._inbox.get(timeout=0.1)
                except queue.Empty:
                    now = time.monotonic()
                    if should_abort and should_abort():
                        self._send_soon({"t": "cancel", "id": job_id})
                        raise JobCancelled()
                    if now - last_sign > limit:
                        raise EngineError(f"No progress for {limit} s")
                    if on_tick and now - last_tick >= 1.0:
                        last_tick = now
                        on_tick(done)
                    continue
                if msg is None:
                    raise EngineError(f"Speech engine ended ({self._exit_note()})")
                if msg.get("id") != job_id:
                    continue  # the end of a cancelled job
                kind = msg["t"]
                if kind == "progress":
                    last_sign = time.monotonic()
                    done = msg["done"]
                elif kind == "result":
                    if msg["result"].get("compute_fallback"):
                        self.compute_type = msg["result"]["compute_type"]
                    return msg["result"]
                elif kind == "cancelled":
                    raise JobCancelled()
                elif kind == "error":
                    raise EngineError(msg["error"])
        finally:
            self._job_lock.release()

    def kill(self):
        self._dead = True
        try:
            self.proc.kill()
            self.proc.wait(timeout=3)
        except Exception as e:
            logger.warning(f"Stopping the speech engine: {e}")


class SpeechEngine:
    """The backend's handle on Whisper. Callbacks (all optional):
    on_ready(info): a model is loaded and tested. on_status(text): load progress.
    on_fallback(): the GPU failed, running on the processor. on_failed(text):
    no model could be loaded at all. is_recording(): nothing heavy while it's on.
    """

    def __init__(self, on_ready=None, on_status=None, on_fallback=None, on_failed=None,
                 is_recording=None):
        self._on_ready = on_ready or (lambda info: None)
        self._on_status = on_status or (lambda text: None)
        self._on_fallback = on_fallback or (lambda: None)
        self._on_failed = on_failed or (lambda text: None)
        self._is_recording = is_recording or (lambda: False)
        self._worker = None
        self._boot_lock = threading.Lock()   # one (re)start at a time
        self._settled = threading.Event()    # a worker is up, or none could be
        self._model_size = None
        self._compute_type = "auto"
        self._has_gpu = False                # seen on this machine since the start
        self._gpu_lost = False               # on the processor because the GPU failed
        self._closing = False
        self.model_size = None
        self.last_device = None

    # ── State ────────────────────────────────────────────────────────
    @property
    def is_loaded(self) -> bool:
        return self._settled.is_set() and self._worker is not None

    @property
    def device(self):
        """Where the model runs (or last ran), None before the first load."""
        return self.last_device

    def get_device_info(self) -> str:
        worker = self._worker
        if worker is None:
            return ""
        return f"{(worker.device or '').upper()} ({worker.compute_type})"

    # ── (Re)starting ─────────────────────────────────────────────────
    def load(self, model_size: str, compute_type: str = "auto"):
        """Load a model (at the start, or the user picked another one). Blocks
        until it is settled; failures end up in on_failed."""
        with self._boot_lock:
            self._model_size, self._compute_type = model_size, compute_type
            self._boot()

    def _spawn(self, device: str, compute_type: str) -> _Worker:
        worker = None
        try:
            worker = _Worker(self._model_size, device, compute_type, self._on_status)
            worker.wait_ready()
            return worker
        except EngineError as e:
            if worker:
                worker.kill()
                e.device = worker.device
            raise
        except OSError as e:
            raise EngineError(f"Could not start the speech engine: {e}")
        finally:
            if worker and worker.device == "cuda":
                self._has_gpu = True

    def _adopt(self, worker: _Worker):
        self._worker = worker
        if worker.compute_fallback:  # no need to find that out on every restart
            self._compute_type = worker.compute_type
        self.model_size = self._model_size
        self.last_device = worker.device
        self._settled.set()
        logger.info(f"Speech engine ready: {self._model_size} on {self.get_device_info()}")
        self._on_ready({"device": self.get_device_info(), "model": self._model_size,
                        "compute_type": worker.compute_type,
                        "compute_fallback": worker.compute_fallback})
        gpu_lost = worker.device == "cpu" and self._has_gpu
        if gpu_lost and not self._gpu_lost:
            self._on_fallback()
            threading.Thread(target=self._recheck_gpu, daemon=True).start()
        self._gpu_lost = gpu_lost

    def _boot(self, force_cpu: bool = False):
        """Replace the running process (call under _boot_lock)."""
        self._settled.clear()
        old, self._worker = self._worker, None
        if old:
            old.kill()
        if self._closing:
            self._settled.set()
            return
        error = None
        if not force_cpu:
            try:
                self._adopt(self._spawn("auto", self._compute_type))
                return
            except EngineError as e:
                error = e
                logger.error(f"Model load failed: {e}")
                if getattr(e, "device", None) == "cpu":  # no GPU in this machine
                    self._give_up(e)
                    return
        try:
            self._adopt(self._spawn("cpu", "auto"))
        except EngineError as e:
            logger.error(f"CPU fallback failed: {e}")
            self._give_up(e if error is None else error)

    def _give_up(self, error):
        self._settled.set()
        self._on_failed(str(error))

    def _replace(self, failed, force_cpu: bool = False):
        """Replace `failed`, unless that has happened in the meantime."""
        with self._boot_lock:
            if self._worker is failed and not self._closing:
                self._boot(force_cpu)
            else:
                self._settled.set()

    def _replace_soon(self, failed, force_cpu: bool = False):
        """In the background, and from this moment on nobody gets `failed`
        anymore: whoever needs the engine waits for the new process."""
        if self._worker is failed:
            self._settled.clear()
        threading.Thread(target=self._replace, args=(failed, force_cpu), daemon=True).start()

    def _recheck_gpu(self):
        """Running on the processor because the GPU failed: try it again now
        and then, without the user having to restart anything. The processor
        process keeps working until the GPU one has proven itself."""
        for delay in GPU_RECHECK_DELAYS:
            time.sleep(delay)
            if self._closing or not self._gpu_lost:
                return
            current = self._worker
            if self._is_recording() or (current and current.busy):
                continue
            try:
                worker = self._spawn("auto", self._compute_type)
            except EngineError as e:
                logger.info(f"GPU still not usable: {e}")
                continue
            with self._boot_lock:
                current = self._worker
                if (self._closing or worker.device != "cuda"
                        or worker.model_size != self._model_size
                        or self._is_recording() or (current and current.busy)):
                    worker.kill()
                    continue
                logger.info("GPU is back, leaving the processor")
                self._adopt(worker)
                if current:
                    current.kill()
                return

    # ── Transcribing ─────────────────────────────────────────────────
    def _limit(self, table: dict, worker: _Worker) -> float:
        return table.get(worker.device, table["cpu"])

    def _wait_worker(self, on_status, should_abort) -> _Worker:
        """The running process, waiting for a (re)start if need be."""
        if self._settled.is_set() and self._worker is None and not self._closing:
            # Nothing could be loaded earlier. This audio is worth a new try.
            self._replace_soon(None)
        last_tick = time.monotonic()
        while not self._settled.wait(timeout=0.2):
            if should_abort and should_abort():
                raise JobCancelled()
            if on_status and time.monotonic() - last_tick >= 1.0:
                last_tick = time.monotonic()
                on_status("restarting", None)
        worker = self._worker
        if worker is None:
            raise EngineError("No model could be loaded")
        return worker

    def transcribe(self, audio, opts: dict, on_status=None, should_abort=None) -> dict:
        """Audio → result, whatever it takes. `on_status(stage, percent)` comes
        about once a second on anything slow. Raises EngineError once all tries
        have failed, JobCancelled when should_abort() said so."""
        total = max(len(audio) / 16000, 0.001)
        error = None
        for attempt in range(ATTEMPTS):
            worker = self._wait_worker(on_status, should_abort)
            started = time.monotonic()

            def on_tick(done):
                if on_status and time.monotonic() - started >= STATUS_AFTER:
                    on_status("working", min(99, int(100 * done / total)))

            try:
                result = worker.run(audio, opts, self._limit(FINAL_LIMIT, worker),
                                    on_tick=on_tick, should_abort=should_abort)
                if attempt:
                    logger.info(f"Recovered on attempt {attempt + 1} ({worker.device})")
                return result
            except EngineError as e:
                error = e
                logger.error(f"Transcription attempt {attempt + 1} failed "
                             f"({worker.device}): {e}")
                if attempt + 1 == ATTEMPTS:
                    self._replace_soon(worker)  # a fresh start for the next dictation
                    break
                if on_status:
                    on_status("restarting", None)
                # A new process that fails as well: the GPU isn't coming back
                # for now.
                self._replace_soon(worker, force_cpu=attempt >= 1)
        raise EngineError(str(error))

    def preview(self, audio, opts: dict):
        """A cheap pass while recording. None when it can't run right now; a
        failure starts the replacement while the user keeps talking, so the
        final pass finds a working process."""
        worker = self._worker
        if not self.is_loaded or worker is None or worker.busy:
            return None
        try:
            return worker.run(audio, opts, self._limit(PREVIEW_LIMIT, worker))
        except EngineError as e:
            logger.warning(f"Preview pass failed, replacing the speech engine: {e}")
            self._replace_soon(worker)
            return None

    def probe(self):
        """Recording started: find out now if a driver reset killed the GPU
        context since the last dictation. It costs about 100 ms in the
        background and gives the replacement a head start."""
        worker = self._worker
        if not self.is_loaded or worker is None or worker.device != "cuda" or worker.busy:
            return

        def run():
            try:
                worker.run(None, {}, PREVIEW_LIMIT["cuda"])
            except EngineError as e:
                logger.warning(f"GPU check failed, replacing the speech engine: {e}")
                self._replace_soon(worker)

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._closing = True
        worker = self._worker
        if worker:
            worker.kill()
