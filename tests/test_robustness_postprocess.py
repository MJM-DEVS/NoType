import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import pytest  # noqa: E402

import _robustness_stubs  # noqa: E402,F401  (puts the repo on sys.path)
import postprocess  # noqa: E402


class _FakeOllama(BaseHTTPRequestHandler):
    """/api/tags of a local stand-in for Ollama; counts every request."""

    def do_GET(self):
        self.server.probes += 1
        if not self.server.up or self.path != "/api/tags":
            self.send_error(503)
            return
        body = json.dumps({"models": [{"name": "m:1"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Clock:
    """Stands in for postprocess.time: a fake monotonic(), the rest is real."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def ollama(monkeypatch):
    """A real HTTP server on a free loopback port, with postprocess pointed
    at it. Independent of how postprocess does its HTTP: nothing is patched
    but the URL and the clock."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    server.up, server.probes = False, 0
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    server.clock = _Clock()
    monkeypatch.setattr(postprocess, "OLLAMA_URL",
                        f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(postprocess, "time", server.clock)
    monkeypatch.setattr(postprocess, "_availability", (0.0, False))
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_negative_verdict_expires_after_unavailable_ttl(ollama):
    assert not postprocess._ollama_available("m:1")
    assert ollama.probes == 1
    ollama.up = True  # Ollama came up (e.g. started by hand)
    ollama.clock.now += postprocess.UNAVAILABLE_TTL_S - 1
    assert not postprocess._ollama_available("m:1")  # still cached
    assert ollama.probes == 1
    ollama.clock.now += 2
    assert postprocess._ollama_available("m:1")  # re-probed after the TTL
    assert ollama.probes == 2


def test_negative_ttl_shorter_than_positive():
    assert 5 < postprocess.UNAVAILABLE_TTL_S < postprocess.AVAILABILITY_TTL_S


def test_positive_verdict_kept_for_the_full_ttl(ollama):
    ollama.up = True
    assert postprocess._ollama_available("m:1")
    ollama.clock.now += postprocess.AVAILABILITY_TTL_S - 1
    assert postprocess._ollama_available("m:1")
    assert ollama.probes == 1
