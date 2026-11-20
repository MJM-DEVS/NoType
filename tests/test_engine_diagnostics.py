"""The diagnostics the speech engine process writes to notype.log at every
start: enough to tell why a model doesn't load on a machine nobody here can
look at, and nothing personal (no host or user name, no paths)."""

import getpass
import os
import platform
import re
import sys
import types

from transcribe_worker import engine_info, format_info, scrub_home, system_info

SYSTEM_KEYS = {"os", "arch", "python", "cpu_threads", "avx", "avx2", "avx512", "ram_gb"}


def personal_values():
    """What must not end up in the log (where long enough to search for)."""
    values = [platform.node(), os.path.expanduser("~")]
    try:
        values.append(getpass.getuser())
    except Exception:
        pass
    return [v for v in values if v and len(v) >= 4]


def test_system_info_says_what_runs_where():
    info = system_info()
    assert {"os", "arch", "python", "cpu_threads"} <= set(info)
    assert info["python"] == platform.python_version()
    assert info["cpu_threads"] == os.cpu_count()
    if sys.platform.startswith("linux"):
        assert isinstance(info["avx"], bool) and isinstance(info["avx2"], bool)
        assert info["ram_gb"] > 0


def test_system_info_has_nothing_personal():
    info = system_info()
    assert set(info) <= SYSTEM_KEYS   # no host name, user name or folder
    text = format_info(info)
    for value in personal_values():
        assert value not in text


def fake_ctranslate2(cuda_devices=0, error=None):
    def count():
        if error:
            raise error
        return cuda_devices

    types_by_device = {"cpu": {"int8", "float32", "int8_float32", "int16"},
                       "cuda": {"float16", "int8_float16", "int8", "float32"}}
    return types.SimpleNamespace(__version__="4.5.0", get_cuda_device_count=count,
                                 get_supported_compute_types=lambda device: types_by_device[device])


def test_engine_info_on_a_machine_without_cuda(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2", fake_ctranslate2())
    info = engine_info()
    assert info["ctranslate2"] == "4.5.0"
    assert info["cuda_devices"] == 0
    assert info["cpu_types"] == "float32/int16/int8/int8_float32"
    assert "cuda_types" not in info


def test_engine_info_with_a_gpu_lists_its_compute_types(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2", fake_ctranslate2(cuda_devices=1))
    info = engine_info()
    assert info["cuda_types"] == "float16/float32/int8/int8_float16"


def test_engine_info_for_the_processor_leaves_cuda_alone(monkeypatch):
    # The CPU process is the fallback for a CUDA start that hung: asking for
    # CUDA devices there could hang it as well.
    def hangs():
        raise AssertionError("CUDA was asked")

    fake = fake_ctranslate2()
    fake.get_cuda_device_count = hangs
    monkeypatch.setitem(sys.modules, "ctranslate2", fake)
    info = engine_info("cpu")
    assert info["cuda_devices"] == "not checked"
    assert info["cpu_types"] == "float32/int16/int8/int8_float32"


def test_engine_info_when_ctranslate2_does_not_load(monkeypatch):
    # A missing DLL: the reason is in the log before anything else fails.
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    info = engine_info()
    assert re.match(r"unavailable \((ModuleNotFound|Import)Error: ", info["ctranslate2"])
    assert "faster_whisper" in info


def test_engine_info_keeps_the_home_folder_out(monkeypatch):
    home = os.path.expanduser("~")
    error = OSError(f"cannot load {home}/AppData/Local/NoType/cudart64_12.dll")
    monkeypatch.setitem(sys.modules, "ctranslate2", fake_ctranslate2(error=error))
    info = engine_info()
    assert home not in info["ctranslate2"]
    assert "~/AppData/Local/NoType/cudart64_12.dll" in info["ctranslate2"]


def test_scrub_home_in_both_spellings(monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda path: r"C:\Users\Mijo")
    assert scrub_home(r"C:\Users\Mijo\.cache\model.bin") == r"~\.cache\model.bin"
    assert scrub_home("c:/users/mijo/.cache/model.bin") == "~/.cache/model.bin"
    assert scrub_home(r"D:\Models\small") == r"D:\Models\small"
    assert scrub_home(OSError("no file")) == "no file"


def test_scrub_home_without_a_usable_home(monkeypatch):
    for home in ("", "~", "/"):
        monkeypatch.setattr(os.path, "expanduser", lambda path, home=home: home)
        assert scrub_home("/models/small") == "/models/small"


def test_format_info_reads_as_one_line():
    assert format_info({"os": "Windows 10", "avx": True, "avx2": False, "cuda_devices": 0}) == \
        "os=Windows 10, avx=yes, avx2=no, cuda_devices=0"
