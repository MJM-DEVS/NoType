"""SpeechEngine without Whisper: how it reacts to a model that won't load.

_boot() runs against a fake _Worker. wait_ready() runs against a real child
process that plays back a script of messages (no model, no GPU libraries),
with the time limits shortened."""

import json
import logging
import os
import re
import sys
import threading
import time
import types

import pytest

import speech_engine
from speech_engine import EngineError, SpeechEngine, describe_load_failure, load_failure_code
from transcriber import resolve_compute_type

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FLOAT16_ON_CPU = ("Requested float16 compute type, but the target device or backend do not "
                  "support efficient float16 computation.")


# ── Classifying a failed load ──

@pytest.mark.parametrize("message, code", [
    ("Model download failed (Systran/faster-whisper-small): Name or service not known", "download"),
    ("Loading the model got stuck (no progress for 600 s)", "stuck"),
    ("Speech engine ended while loading (exit code 3221225477)", "crashed"),   # access violation
    ("Speech engine ended while loading (exit code 1)", "crashed"),
    ("Speech engine is gone: [Errno 32] Broken pipe", "crashed"),
    # A processor without AVX: Windows STATUS_ILLEGAL_INSTRUCTION, SIGILL.
    ("Speech engine ended while loading (exit code 3221225501)", "cpu_unsupported"),
    ("Speech engine ended while loading (exit code -4)", "cpu_unsupported"),
    ("Fatal Python error: Illegal instruction", "cpu_unsupported"),
    # Missing or blocked files of the install.
    ("Speech engine ended while loading (exit code 3221225781)", "runtime"),  # DLL not found
    ("DLL load failed while importing _ext: The specified module could not be found.", "runtime"),
    ("No module named 'ctranslate2'", "runtime"),
    ("Library cublas64_12.dll is not found or cannot be loaded", "runtime"),
    ("Could not start the speech engine: [WinError 2]", "runtime"),
    (FLOAT16_ON_CPU + " (model small on cpu, tried int8, float32)", "compute_type"),
    ("CUDA failed with error out of memory", "memory"),
    ("std::bad_alloc", "memory"),
    ("Speech engine failed while loading (KeyError: 'download')", "other"),
    ("", "other"),
    (None, "other"),
])
def test_load_failure_code(message, code):
    assert load_failure_code(message) == code


def test_exit_codes_are_matched_whole():
    # 3221225501 inside a longer number is no illegal instruction.
    assert load_failure_code("Speech engine ended while loading (exit code 32212255010)") == "crashed"
    assert load_failure_code("Speech engine ended while loading (exit code -41)") == "crashed"


def test_hint_says_cpu_mode_without_nvidia():
    hint = describe_load_failure("Loading the model got stuck", "cpu", gpu_found=False)["hint"]
    assert hint.startswith("No NVIDIA GPU found")
    assert "processor" in hint


def test_hint_without_cpu_note_on_a_gpu_machine():
    assert "NVIDIA" not in describe_load_failure("x", "cuda", gpu_found=True)["hint"]
    assert "NVIDIA" not in describe_load_failure("x", "cpu", gpu_found=True)["hint"]


def test_every_code_has_a_hint():
    assert set(speech_engine.LOAD_HINTS) == {
        "compute_type", "download", "stuck", "crashed", "memory", "cpu_unsupported", "runtime",
        "other"}


def test_codes_match_the_electron_side():
    # electron/lib/model-status.js words each code in both languages; the
    # backend-only "backend" code is Electron's own (backend crash loop).
    with open(os.path.join(ROOT, "electron", "lib", "model-status.js"), encoding="utf-8") as f:
        js = f.read()
    codes = json.loads(re.search(r"const CODES = (\[[^\]]*\]);", js).group(1).replace("'", '"'))
    assert set(codes) == set(speech_engine.LOAD_HINTS) | {"backend"}


def test_hints_point_to_try_again_where_that_helps():
    for code in ("download", "stuck", "crashed"):
        assert "try again" in speech_engine.LOAD_HINTS[code]


# ── _boot against a fake worker ──

class FakeWorker:
    """Stands in for _Worker. `plan(worker)` plays its wait_ready(): set
    device/compute_type like the "loading" message would, then return or
    raise EngineError."""
    plan = None
    spawned = []

    def __init__(self, model_size, device, compute_type, on_status=None):
        self.model_size = model_size
        self.requested = (device, compute_type)
        self.device = None if device == "auto" else device
        self.compute_type = compute_type
        self.compute_fallback = False
        self.busy = False
        self.killed = False
        FakeWorker.spawned.append(self)

    def wait_ready(self):
        FakeWorker.plan(self)

    def kill(self):
        self.killed = True


@pytest.fixture
def boot(monkeypatch):
    """boot(plan, compute_type) → loads 'small' and returns what happened."""
    monkeypatch.setattr(speech_engine, "_Worker", FakeWorker)
    monkeypatch.setattr(speech_engine, "GPU_RECHECK_DELAYS", ())
    FakeWorker.spawned = []

    def run(plan, compute_type="auto", on_ready=None):
        FakeWorker.plan = staticmethod(plan)
        seen = types.SimpleNamespace(ready=[], failed=[], fallback=0, fallback_codes=[], events=[])

        def fallback(info):
            seen.fallback += 1
            seen.fallback_codes.append(info["code"])

        def ready(info):
            seen.ready.append(info)
            seen.events.append("ready")
            if on_ready:
                on_ready(info)

        def failed(info):
            seen.failed.append(info)
            seen.events.append("failed")

        engine = SpeechEngine(on_ready=ready, on_failed=failed, on_fallback=fallback,
                              on_loading=lambda info: seen.events.append(("loading", info["model"])))
        engine.load("small", compute_type)
        seen.engine = engine
        seen.spawned = [w.requested for w in FakeWorker.spawned]
        seen.workers = list(FakeWorker.spawned)
        return seen

    return run


def no_gpu(worker):
    """A machine without CUDA: the worker lands on the processor and loads
    with what the transcriber makes of the compute type."""
    worker.device = "cpu"
    worker.compute_type = resolve_compute_type("cpu", worker.compute_type)


def test_no_gpu_with_persisted_float16_loads_at_once(boot):
    seen = boot(no_gpu, compute_type="float16")
    assert seen.spawned == [("auto", "float16")]
    assert seen.failed == []
    assert seen.ready[0]["compute_type"] == "int8"
    assert seen.ready[0]["device"] == "CPU (int8)"
    assert seen.engine.is_loaded


def test_cuda_failure_falls_back_to_cpu(boot):
    def plan(worker):
        if worker.requested[0] == "auto":
            worker.device = "cuda"
            raise EngineError("Speech engine ended while loading (exit code 3221225477)")
        no_gpu(worker)

    seen = boot(plan)
    assert seen.spawned == [("auto", "auto"), ("cpu", "auto")]
    assert seen.workers[0].killed
    assert seen.ready[0]["device"] == "CPU (int8)"
    assert seen.fallback == 1
    assert seen.fallback_codes == ["crashed"]
    assert seen.failed == []


def test_too_little_graphics_memory_says_so_on_fallback(boot):
    # CUDA works, the model doesn't fit (float16 and int8 both out of memory).
    def plan(worker):
        if worker.requested[0] == "auto":
            worker.device = "cuda"
            raise EngineError("CUDA failed with error out of memory (model large-v3 on cuda, "
                              "tried float16, int8)")
        no_gpu(worker)

    seen = boot(plan)
    assert seen.ready[0]["device"] == "CPU (int8)"
    assert seen.fallback_codes == ["memory"]


def test_cpu_with_a_gpu_compute_type_tries_cpu_auto(boot):
    # A worker that kept float16 on the processor (as before this fix).
    def plan(worker):
        worker.device = "cpu"
        if worker.compute_type == "float16":
            raise EngineError(FLOAT16_ON_CPU)
        no_gpu(worker)

    seen = boot(plan, compute_type="float16")
    assert seen.spawned == [("auto", "float16"), ("cpu", "auto")]
    assert seen.ready[0]["compute_type"] == "int8"
    assert seen.failed == []


def test_cpu_int8_failure_gives_up_without_a_second_try(boot):
    def plan(worker):
        no_gpu(worker)
        raise EngineError("Loading the model got stuck (no progress for 600 s)")

    seen = boot(plan)
    assert seen.spawned == [("auto", "auto")]
    assert seen.failed == [{"message": "Loading the model got stuck (no progress for 600 s)",
                            "model": "small", "device": "cpu", "compute_type": "int8"}]
    assert not seen.engine.is_loaded
    assert seen.engine._settled.is_set()   # nobody waits for it forever


def test_failed_download_is_not_retried_on_the_cpu(boot):
    def plan(worker):
        worker.device = "cpu"
        worker.compute_type = "float32"
        raise EngineError("Model download failed (small): timed out")

    seen = boot(plan, compute_type="float32")
    assert seen.spawned == [("auto", "float32")]
    assert load_failure_code(seen.failed[0]["message"]) == "download"


def test_both_failing_reports_the_first_error_once(boot):
    def plan(worker):
        if worker.requested[0] == "auto":
            worker.device = "cuda"
            raise EngineError("Library cublas64_12.dll is not found")
        no_gpu(worker)
        raise EngineError("Speech engine ended while loading (exit code 1)")

    seen = boot(plan)
    assert seen.spawned == [("auto", "auto"), ("cpu", "auto")]
    assert len(seen.failed) == 1
    assert seen.failed[0]["device"] == "cuda"
    assert "cublas64_12" in seen.failed[0]["message"]
    assert seen.ready == []


def test_process_that_died_before_its_device_was_known_gets_the_cpu(boot):
    def plan(worker):
        if worker.requested[0] == "auto":
            raise EngineError("Loading the model got stuck (no progress for 120 s)")  # CUDA probe hung
        no_gpu(worker)

    seen = boot(plan)
    assert seen.spawned == [("auto", "auto"), ("cpu", "auto")]
    assert seen.ready and not seen.failed


# ── Every load ends in ready or failed ──

def test_loading_is_announced_before_the_end(boot):
    assert boot(no_gpu).events == [("loading", "small"), "ready"]

    def fails(worker):
        no_gpu(worker)
        raise EngineError("Speech engine ended while loading (exit code 1)")

    assert boot(fails).events == [("loading", "small"), "failed"]


def test_a_bug_while_loading_still_ends_in_failed(boot):
    # Not an EngineError (a malformed message, say): the processor gets its
    # try, and when that fails too the UI hears about it.
    def plan(worker):
        raise KeyError("download")

    seen = boot(plan)
    assert seen.spawned == [("auto", "auto"), ("cpu", "auto")]
    assert all(w.killed for w in seen.workers)
    assert len(seen.failed) == 1
    assert seen.failed[0]["message"] == "Speech engine failed while loading (KeyError: 'download')"
    assert seen.engine._settled.is_set()


def test_a_bug_on_the_gpu_falls_back_to_the_processor(boot):
    def plan(worker):
        if worker.requested[0] == "auto":
            worker.device = "cuda"
            raise TypeError("unexpected message")
        no_gpu(worker)

    seen = boot(plan)
    assert seen.ready[0]["device"] == "CPU (int8)"
    assert not seen.failed


def test_an_error_outside_the_workers_is_reported(boot, monkeypatch):
    def broken_spawn(self, device, compute_type):
        raise RuntimeError("no luck")

    monkeypatch.setattr(SpeechEngine, "_spawn", broken_spawn)
    seen = boot(no_gpu)
    assert seen.events == [("loading", "small"), "failed"]
    assert seen.failed[0]["message"] == "no luck"
    assert seen.engine._settled.is_set()


def test_a_failing_ready_callback_is_no_failed_load(boot):
    def on_ready(info):
        raise OSError("settings.json is locked")

    seen = boot(no_gpu, on_ready=on_ready)
    assert seen.failed == []
    assert seen.engine.is_loaded


def test_starting_the_process_fails(boot, monkeypatch):
    class CantStart(FakeWorker):
        def __init__(self, *args, **kwargs):
            raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(speech_engine, "_Worker", CantStart)
    seen = boot(no_gpu)
    assert seen.failed[0]["message"].startswith("Could not start the speech engine")
    assert load_failure_code(seen.failed[0]["message"]) == "runtime"


def test_the_home_folder_is_not_in_the_failure(boot, monkeypatch):
    home = os.path.expanduser("~")

    def plan(worker):
        no_gpu(worker)
        raise EngineError(f"Unable to open file 'model.bin' in model '{home}/models/small'")

    seen = boot(plan)
    assert home not in seen.failed[0]["message"]
    assert "'~/models/small'" in seen.failed[0]["message"]


# ── Try again ──

def test_retry_after_a_failure_loads_again(boot):
    attempts = []

    def plan(worker):
        no_gpu(worker)
        attempts.append(worker)
        if len(attempts) == 1:
            raise EngineError("Model download failed (small): timed out")

    seen = boot(plan)
    assert seen.events == [("loading", "small"), "failed"]
    assert seen.engine.retry() is True
    assert seen.events == [("loading", "small"), "failed", ("loading", "small"), "ready"]
    assert seen.engine.is_loaded


def test_retry_does_nothing_while_a_model_runs(boot):
    seen = boot(no_gpu)
    assert seen.engine.retry() is False
    assert len(FakeWorker.spawned) == 1
    assert seen.events == [("loading", "small"), "ready"]


def test_retry_does_nothing_while_a_load_is_under_way(boot):
    def fails(worker):
        no_gpu(worker)
        raise EngineError("Loading the model got stuck (no progress for 600 s)")

    seen = boot(fails)
    with seen.engine._boot_lock:  # someone else is loading right now
        assert seen.engine.retry() is False
    assert len(FakeWorker.spawned) == 1


def test_retry_before_any_load_does_nothing():
    assert SpeechEngine().retry() is False


def test_two_quick_retries_load_once(boot, monkeypatch):
    def fails(worker):
        no_gpu(worker)
        raise EngineError("Loading the model got stuck (no progress for 600 s)")

    seen = boot(fails)
    gate = threading.Event()

    def slow(worker):
        gate.wait(5)
        no_gpu(worker)

    FakeWorker.plan = staticmethod(slow)
    results = []
    first = threading.Thread(target=lambda: results.append(seen.engine.retry()))
    first.start()
    while not seen.engine._boot_lock.locked():
        time.sleep(0.01)
    results.append(seen.engine.retry())   # pressed again while the first one loads
    gate.set()
    first.join(5)
    assert sorted(results) == [False, True]
    assert len(FakeWorker.spawned) == 2   # the failed load and one retry


def test_a_dictation_after_a_failure_announces_its_new_try(boot):
    def plan(worker):
        no_gpu(worker)
        if len(FakeWorker.spawned) == 1:
            raise EngineError("Loading the model got stuck (no progress for 600 s)")

    seen = boot(plan)
    seen.engine._replace(None)    # what _wait_worker() starts for a rescue run
    assert seen.events == [("loading", "small"), "failed", ("loading", "small"), "ready"]


# ── wait_ready against a scripted child process ──

CHILD = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from transcribe_worker import read_frame, write_frame
read_frame(sys.stdin.buffer)  # init
for step in json.loads(sys.argv[2]):
    if "sleep" in step:
        time.sleep(step["sleep"])
    elif "exit" in step:
        sys.exit(step["exit"])
    else:
        write_frame(sys.stdout.buffer, step)
time.sleep(60)
"""


@pytest.fixture
def scripted(monkeypatch):
    """scripted(steps, device) → a _Worker whose process plays `steps`."""
    workers = []

    def start(steps, device="auto", compute_type="auto"):
        monkeypatch.setattr(speech_engine, "_worker_command",
                            lambda: [sys.executable, "-c", CHILD, ROOT, json.dumps(steps)])
        statuses = []
        worker = speech_engine._Worker("small", device, compute_type, on_status=statuses.append)
        worker.statuses = statuses
        workers.append(worker)
        return worker

    yield start
    for worker in workers:
        worker.kill()


def loading(device="cpu", compute_type="int8", download=False):
    return {"t": "loading", "device": device, "compute_type": compute_type, "download": download}


def ready(device="cpu", compute_type="int8"):
    return {"t": "ready", "device": device, "compute_type": compute_type, "compute_fallback": False}


def test_status_lines_keep_a_slow_load_alive(monkeypatch, scripted):
    monkeypatch.setattr(speech_engine, "LOAD_LIMIT", {"cuda": 1.0, "cpu": 1.0})
    steps = [loading()]
    for i in range(6):                     # 2.4 s in all, well past the limit
        steps += [{"sleep": 0.4}, {"t": "status", "msg": f"step {i}"}]
    worker = scripted(steps + [ready()])
    worker.wait_ready()
    assert worker.device == "cpu"
    assert len(worker.statuses) == 6


def test_no_progress_raises_a_clear_error_and_ends_the_process(monkeypatch, scripted):
    monkeypatch.setattr(speech_engine, "LOAD_LIMIT", {"cuda": 0.6, "cpu": 0.6})
    worker = scripted([loading()])
    started = time.monotonic()
    with pytest.raises(EngineError, match="Loading the model got stuck"):
        worker.wait_ready()
    assert time.monotonic() - started < 5
    assert worker.proc.poll() is not None
    assert load_failure_code("Loading the model got stuck") == "stuck"


def test_cpu_gets_the_longer_load_limit(monkeypatch, scripted):
    monkeypatch.setattr(speech_engine, "LOAD_LIMIT", {"cuda": 0.5, "cpu": 10})
    worker = scripted([{"sleep": 1.5}, loading(), ready()], device="cpu")
    worker.wait_ready()
    assert worker.device == "cpu"


def test_auto_is_held_to_the_gpu_limit_until_loading(monkeypatch, scripted):
    # A CUDA start that hangs before the device is known.
    monkeypatch.setattr(speech_engine, "LOAD_LIMIT", {"cuda": 0.5, "cpu": 10})
    worker = scripted([{"sleep": 30}])
    with pytest.raises(EngineError, match="got stuck"):
        worker.wait_ready()


def test_download_heartbeats_on_status_too(monkeypatch, scripted):
    monkeypatch.setattr(speech_engine, "DOWNLOAD_LIMIT", 1.0)
    steps = [loading(download=True)]
    for i in range(5):
        steps += [{"sleep": 0.4}, {"t": "status", "msg": "downloading"}]
    worker = scripted(steps + [ready()])
    worker.wait_ready()


def test_process_ending_while_loading(scripted):
    worker = scripted([loading(), {"exit": 3}])
    with pytest.raises(EngineError, match="ended while loading") as info:
        worker.wait_ready()
    assert load_failure_code(str(info.value)) == "crashed"


def test_load_failed_message_is_passed_on(scripted):
    worker = scripted([loading(), {"t": "load_failed", "error": FLOAT16_ON_CPU}])
    with pytest.raises(EngineError, match="float16 compute type"):
        worker.wait_ready()


def test_load_failed_before_loading_still_tells_the_device(scripted):
    worker = scripted([{"t": "load_failed", "error": "No module named 'faster_whisper'",
                        "device": "cpu", "compute_type": "int8"}])
    with pytest.raises(EngineError):
        worker.wait_ready()
    assert (worker.device, worker.compute_type) == ("cpu", "int8")


# ── The real speech engine process, without the inference stack ──

# A broken install on a PC without NVIDIA (a virus scanner took a DLL, …):
# ctranslate2 and faster-whisper don't import. This runs transcribe_worker.main()
# for real; on CI they aren't installed anyway, the finder makes it certain.
CHILD_WORKER = r"""
import importlib.abc, sys
class Missing(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("ctranslate2", "faster_whisper"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Missing())
sys.path.insert(0, sys.argv[1])
import transcribe_worker
transcribe_worker.main()
"""


def test_missing_inference_stack_is_a_runtime_failure_with_diagnostics(monkeypatch, caplog):
    monkeypatch.setattr(speech_engine, "_worker_command",
                        lambda: [sys.executable, "-c", CHILD_WORKER, ROOT])
    failed, loading_events = [], []
    engine = SpeechEngine(on_failed=failed.append, on_loading=loading_events.append)
    with caplog.at_level(logging.INFO):
        engine.load("small", "float16")   # what the settings window used to save
    engine.stop()
    assert loading_events == [{"model": "small"}]
    assert len(failed) == 1
    assert failed[0]["message"] == "No module named 'faster_whisper'"
    assert (failed[0]["device"], failed[0]["compute_type"]) == ("cpu", "int8")
    assert load_failure_code(failed[0]["message"]) == "runtime"
    # Failed on the processor with int8: no second, identical try.
    assert caplog.text.count("System: os=") == 1
    # What the log tells about this machine, and why it runs on the processor.
    assert "cpu_threads=" in caplog.text
    assert "ctranslate2=unavailable (ModuleNotFoundError: No module named 'ctranslate2')" in caplog.text
    assert "CUDA check failed, using the processor" in caplog.text
    assert "compute_type=float16 does not run on the processor, using int8" in caplog.text
