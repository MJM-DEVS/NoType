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
import platform
import queue
import re
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


# ── Diagnostics ──────────────────────────────────────────────────────
# Two log lines per process start, so notype.log tells why a model doesn't
# load on a machine nobody here can look at. Nothing personal goes in: no
# host or user name, no paths.

def scrub_home(text: str) -> str:
    """`text` with the user's home folder written as ~ (error messages carry
    paths, and with them the user name)."""
    home = os.path.expanduser("~")
    text = str(text)
    if not home or home == "~" or len(home) < 4:
        return text
    backslashed = home.replace("/", "\\")
    # The last one is how an OSError prints a file name: repr, \ doubled.
    variants = {home, home.replace("\\", "/"), backslashed, backslashed.replace("\\", "\\\\")}
    for variant in sorted(variants, key=len, reverse=True):
        text = re.sub(re.escape(variant), "~", text, flags=re.IGNORECASE)
    return text


def _cpu_features() -> dict:
    """AVX, AVX2, AVX-512 present? Empty where the OS doesn't say."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            present = ctypes.windll.kernel32.IsProcessorFeaturePresent
            # PF_AVX_INSTRUCTIONS_AVAILABLE, PF_AVX2_…, PF_AVX512F_…
            return {"avx": bool(present(39)), "avx2": bool(present(40)), "avx512": bool(present(41))}
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("flags"):
                    flags = set(line.split(":", 1)[1].split())
                    return {"avx": "avx" in flags, "avx2": "avx2" in flags, "avx512": "avx512f" in flags}
    except Exception:
        pass
    return {}


def _ram_gb():
    """Physical memory in GB, None when unknown."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_extended", ctypes.c_ulonglong)]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            total = status.total
        else:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return round(total / 2 ** 30, 1)
    except Exception:
        return None


def system_info() -> dict:
    """OS, processor and memory. Needs nothing beyond the standard library,
    so it is in the log even when the inference stack crashes on import."""
    info = {"os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "arch": platform.machine(), "python": platform.python_version(),
            "cpu_threads": os.cpu_count()}
    info.update(_cpu_features())
    ram = _ram_gb()
    if ram:
        info["ram_gb"] = ram
    return info


def engine_info(device: str = "auto") -> dict:
    """Versions of the inference stack and the devices and compute types
    CTranslate2 sees. A process meant for the processor leaves CUDA alone:
    it is often the fallback for a CUDA start that hung."""
    info = {}
    try:
        import ctranslate2
        info["ctranslate2"] = getattr(ctranslate2, "__version__", "?")
        cuda = 0 if device == "cpu" else ctranslate2.get_cuda_device_count()
        info["cuda_devices"] = "not checked" if device == "cpu" else cuda
        for kind in ("cpu", "cuda") if cuda else ("cpu",):
            info[f"{kind}_types"] = "/".join(sorted(ctranslate2.get_supported_compute_types(kind)))
    except Exception as e:
        info["ctranslate2"] = f"unavailable ({e.__class__.__name__}: {scrub_home(e)})"
    try:
        import faster_whisper
        info["faster_whisper"] = getattr(faster_whisper, "__version__", "?")
    except Exception as e:
        info["faster_whisper"] = f"unavailable ({e.__class__.__name__})"
    try:  # runs the VAD in front of every dictation
        import onnxruntime
        info["onnxruntime"] = getattr(onnxruntime, "__version__", "?")
    except Exception as e:
        info["onnxruntime"] = f"unavailable ({e.__class__.__name__}: {scrub_home(e)})"
    return info


def warm_vad():
    """Load the Silero VAD (onnxruntime) now instead of at the first
    dictation. Every dictation runs it, so an onnxruntime that can't load on
    this machine (a DLL a virus scanner took, an older VC++ runtime loaded
    first) would otherwise fail each one after the model said "ready", with
    nothing to tell why. faster-whisper keeps the model it loads here."""
    from faster_whisper import vad
    get_vad_model = getattr(vad, "get_vad_model", None)
    if get_vad_model:
        get_vad_model()


def format_info(info: dict) -> str:
    def text(value):
        return "yes" if value is True else "no" if value is False else str(value)

    return ", ".join(f"{key}={text(value)}" for key, value in info.items())


class _LogToParent(logging.Handler):
    def __init__(self, send):
        super().__init__()
        self._send = send

    def emit(self, record):
        # Every line, tracebacks included, with the home folder as ~:
        # messages and paths of the inference stack carry the user name.
        try:
            self._send({"t": "log", "name": record.name, "level": record.levelno,
                        "msg": scrub_home(self.format(record))})
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
    logger.info(f"System: {format_info(system_info())}")

    engine = None
    try:
        logger.info(f"Speech engine: {format_info(engine_info(init['device']))}")
        from transcriber import JobCancelled, Transcriber

        engine = Transcriber(model_size=init["model_size"], device=init["device"],
                             compute_type=init["compute_type"])
        send({"t": "loading", "device": engine.device, "compute_type": engine.compute_type,
              "download": engine.needs_download()})
        engine.load_model(on_progress=lambda m: send({"t": "status", "msg": m}))
        warm_vad()
        fallback = False
        if engine.device == "cuda":
            fallback = bool(_selftest(engine).get("compute_fallback"))
            logger.info("Inference selftest OK")
    except Exception as e:
        logger.error(f"Model load failed: {scrub_home(e)}", exc_info=True)
        # The device, even when this failed before "loading" said it: the
        # processor isn't tried a second time with what just failed there.
        send({"t": "load_failed", "error": scrub_home(e),
              "device": getattr(engine, "device", None),
              "compute_type": getattr(engine, "compute_type", None)})
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
