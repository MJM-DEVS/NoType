"""SpeechEngine without Whisper: how it reacts to a model that won't load.

_boot() runs against a fake _Worker. wait_ready() runs against a real child
process that plays back a script of messages (no model, no GPU libraries),
with the time limits shortened."""

import json
import os
import sys
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
    ("Speech engine ended while loading (exit code 3221225477)", "crashed"),
    ("Could not start the speech engine: [WinError 2]", "crashed"),
    (FLOAT16_ON_CPU + " (model small on cpu, tried int8, float32)", "compute_type"),
    ("CUDA failed with error out of memory", "memory"),
    ("std::bad_alloc", "memory"),
    ("Library cublas64_12.dll is not found or cannot be loaded", "other"),
    ("", "other"),
])
def test_load_failure_code(message, code):
    assert load_failure_code(message) == code


def test_hint_says_cpu_mode_without_nvidia():
    hint = describe_load_failure("Loading the model got stuck", "cpu", gpu_found=False)["hint"]
    assert hint.startswith("No NVIDIA GPU found")
    assert "processor" in hint


def test_hint_without_cpu_note_on_a_gpu_machine():
    assert "NVIDIA" not in describe_load_failure("x", "cuda", gpu_found=True)["hint"]
    assert "NVIDIA" not in describe_load_failure("x", "cpu", gpu_found=True)["hint"]


def test_every_code_has_a_hint():
    assert set(speech_engine.LOAD_HINTS) == {
        "compute_type", "download", "stuck", "crashed", "memory", "other"}


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

    def run(plan, compute_type="auto"):
        FakeWorker.plan = staticmethod(plan)
        seen = types.SimpleNamespace(ready=[], failed=[], fallback=0)

        def fallback():
            seen.fallback += 1

        engine = SpeechEngine(on_ready=seen.ready.append, on_failed=seen.failed.append,
                              on_fallback=fallback)
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
    assert seen.failed == []


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
