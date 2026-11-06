"""
NoType – Speech engine process.

Runs Whisper in a process of its own (backend.exe --transcribe-worker), so
that what can go wrong in there stays in there. A GPU driver reset leaves the
CUDA context of every running process dead for good, and only a new process
works again; a native crash would otherwise take the recording with it.
speech_engine.py starts, watches and replaces this process.

The two talk over stdin/stdout in length-prefixed pickles. Log lines travel
the same way, because only one process can own the rotating log file.
"""

import faulthandler
import io
import logging
import os
import pickle
import queue
import sys
import threading
import time

_LENGTH_BYTES = 4


def write_frame(stream, obj) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(len(data).to_bytes(_LENGTH_BYTES, "little") + data)
    stream.flush()


def read_frame(stream):
    """The next message, None when the other side is gone."""
    head = stream.read(_LENGTH_BYTES)
    if len(head) < _LENGTH_BYTES:
        return None
    size = int.from_bytes(head, "little")
    data = stream.read(size)
    if len(data) < size:
        return None
    return pickle.loads(data)


class _LogToParent(logging.Handler):
    def __init__(self, send):
        super().__init__()
        self._send = send

    def emit(self, record):
        try:
            self._send({"t": "log", "name": record.name, "level": record.levelno,
                        "msg": self.format(record)})
        except Exception:
            pass


def _take_over_stdio():
    """Keep the pipes for messages and point fd 0/1 at NUL: native libraries
    print to stdout, and one stray byte would break the framing. stderr stays
    a pipe, so a native crash report reaches the log."""
    ipc_in = os.fdopen(os.dup(0), "rb")
    ipc_out = os.fdopen(os.dup(1), "wb")
    nul = os.open(os.devnull, os.O_RDWR)
    os.dup2(nul, 0)
    os.dup2(nul, 1)
    if sys.platform.startswith("win"):  # in a GUI build dup2 leaves these alone
        import ctypes
        import msvcrt
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(nul))
        ctypes.windll.kernel32.SetStdHandle(-10, handle)
        ctypes.windll.kernel32.SetStdHandle(-11, handle)
    sys.stdin = None
    sys.stdout = io.TextIOWrapper(os.fdopen(os.dup(nul), "wb"), encoding="utf-8")
    if sys.stderr is None:  # GUI subsystem build
        sys.stderr = io.TextIOWrapper(os.fdopen(2, "wb", buffering=0, closefd=False),
                                      encoding="utf-8", errors="replace",
                                      line_buffering=True)
    return ipc_in, ipc_out


def _selftest(engine):
    """Half a second of silence through the model with the VAD off, so the
    GPU path really runs. Some GPUs accept a compute type at load time and
    reject it at inference time; transcribe() answers that with float16. Any
    other failure here means the GPU can't be used right now."""
    import numpy as np
    return engine.transcribe(np.zeros(8000, dtype=np.float32),
                             languages=[("de", 1.0)], allow_other=False,
                             beam_size="1", vad_filter=False)


def main():
    ipc_in, ipc_out = _take_over_stdio()
    faulthandler.enable(file=sys.stderr, all_threads=True)

    send_lock = threading.Lock()

    def send(obj):
        with send_lock:
            write_frame(ipc_out, obj)

    handler = _LogToParent(send)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logger = logging.getLogger("NoType.Worker")

    jobs = queue.SimpleQueue()
    cancelled = set()

    def read_commands():
        while True:
            msg = read_frame(ipc_in)
            # Parent gone: leave at once, also in the middle of an inference,
            # so a stuck child can't sit on the GPU memory.
            if msg is None or msg["t"] == "quit":
                os._exit(0)
            if msg["t"] == "job":
                jobs.put(msg)
            elif msg["t"] == "cancel":
                cancelled.add(msg["id"])

    init = read_frame(ipc_in)
    if init is None:
        return
    threading.Thread(target=read_commands, daemon=True).start()

    try:
        from transcriber import JobCancelled, Transcriber

        engine = Transcriber(model_size=init["model_size"], device=init["device"],
                             compute_type=init["compute_type"])
        send({"t": "loading", "device": engine.device, "compute_type": engine.compute_type,
              "download": engine.needs_download()})
        engine.load_model(on_progress=lambda m: send({"t": "status", "msg": m}))
        fallback = False
        if engine.device == "cuda":
            fallback = bool(_selftest(engine).get("compute_fallback"))
            logger.info("Inference selftest OK")
    except Exception as e:
        logger.error(f"Model load failed: {e}", exc_info=True)
        send({"t": "load_failed", "error": str(e)})
        return

    send({"t": "ready", "device": engine.device, "compute_type": engine.compute_type,
          "compute_fallback": fallback})

    while True:
        job = jobs.get()
        job_id = job["id"]
        last_sent = [0.0]

        def on_segment(end):
            if job_id in cancelled:
                raise JobCancelled()
            now = time.monotonic()
            if now - last_sent[0] >= 0.5:
                last_sent[0] = now
                send({"t": "progress", "id": job_id, "done": float(end)})

        try:
            if job.get("probe"):
                result = _selftest(engine)
            else:
                result = engine.transcribe(job["audio"], on_segment=on_segment, **job["opts"])
            send({"t": "result", "id": job_id, "result": result})
        except JobCancelled:
            send({"t": "cancelled", "id": job_id})
        except Exception as e:
            logger.error(f"Inference failed: {e}", exc_info=True)
            send({"t": "error", "id": job_id, "error": str(e)})
        finally:
            cancelled.discard(job_id)
