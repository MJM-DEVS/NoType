import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

import io  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

import _robustness_stubs  # noqa: E402,F401  (puts the repo on sys.path)
import postprocess  # noqa: E402


@pytest.fixture
def clock(monkeypatch):
    """Fake monotonic clock plus a fake Ollama /api/tags (no network)."""
    state = {"now": 1000.0, "up": False, "probes": 0}

    def urlopen(url, timeout=None):
        state["probes"] += 1
        if not state["up"]:
            raise OSError("connection refused")
        return io.BytesIO(json.dumps({"models": [{"name": "m:1"}]}).encode())

    monkeypatch.setattr(postprocess.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(postprocess.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(postprocess, "_availability", (0.0, False))
    return state


def test_negative_verdict_expires_after_seconds(clock):
    assert not postprocess._ollama_available("m:1")
    clock["up"] = True  # Ollama came up (e.g. started by hand)
    clock["now"] += 2
    assert not postprocess._ollama_available("m:1")  # still cached
    assert clock["probes"] == 1
    clock["now"] += 4
    assert postprocess._ollama_available("m:1")  # re-probed after ~5 s
    assert clock["probes"] == 2


def test_positive_verdict_kept_for_the_full_ttl(clock):
    clock["up"] = True
    assert postprocess._ollama_available("m:1")
    clock["now"] += postprocess.AVAILABILITY_TTL_S - 1
    assert postprocess._ollama_available("m:1")
    assert clock["probes"] == 1
