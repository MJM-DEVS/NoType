"""Loopback traffic (Ollama, a local agent webhook) never goes via a proxy;
the agent token never goes over plain http to a public host."""

import os
import sys
import tempfile

# Before config is imported (it creates its folder on import).
_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["XDG_CONFIG_HOME"] = _TMP
os.environ["APPDATA"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http.server  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import urllib.request  # noqa: E402

import pytest  # noqa: E402

import agent_webhook  # noqa: E402
import loopback  # noqa: E402
import ollama_manager  # noqa: E402
import postprocess  # noqa: E402


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers every GET/POST with a small JSON body and records the path."""

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.hits.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path.endswith("/api/pull"):
            body = b'{"status": "success"}\n'
        else:
            body = json.dumps(self.server.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


def _serve(reply=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.hits = []
    server.reply = reply or {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def ollama():
    server = _serve({"version": "0.0.1", "models": [{"name": "m:1"}],
                     "message": {"content": "ok"}})
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(monkeypatch):
    """A dummy proxy on its own port; every request it sees is a leak."""
    server = _serve()
    url = f"http://127.0.0.1:{server.server_port}"
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield server
    server.shutdown()
    server.server_close()


def _point_at(monkeypatch, server):
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(postprocess, "OLLAMA_URL", url)
    monkeypatch.setattr(ollama_manager, "OLLAMA_URL", url)
    postprocess.reset_availability_cache()


def test_default_urllib_would_use_the_proxy(proxy, ollama):
    # The premise of the fix: urllib sends 127.0.0.1 to the system proxy.
    opener = urllib.request.build_opener()
    with opener.open(f"http://127.0.0.1:{ollama.server_port}/api/version", timeout=2):
        pass
    assert proxy.hits and not ollama.hits


def test_postprocess_bypasses_proxy(proxy, ollama, monkeypatch):
    _point_at(monkeypatch, ollama)
    assert postprocess._ollama_available("m:1")
    assert postprocess._model_loaded("m:1")
    assert postprocess._chat({"model": "m:1"}, timeout=2)["message"]["content"] == "ok"
    monkeypatch.setattr(postprocess, "_ollama_available", lambda model: True)
    postprocess.warm_up("m:1")
    paths = [p for _, p, _ in ollama.hits]
    assert paths == ["/api/tags", "/api/ps", "/api/chat", "/api/generate"]
    assert proxy.hits == []


def test_ollama_manager_bypasses_proxy(proxy, ollama, monkeypatch):
    _point_at(monkeypatch, ollama)
    assert ollama_manager.api_running()
    assert ollama_manager.running_version() == "0.0.1"
    assert ollama_manager.model_pulled("m:1")
    assert ollama_manager.pull_model("m:1")
    assert len(ollama.hits) == 4
    assert proxy.hits == []


def test_loopback_urlopen_refuses_other_hosts():
    with pytest.raises(ValueError):
        loopback.urlopen("http://example.com/", timeout=1)


@pytest.mark.parametrize("host,local", [
    ("127.0.0.1", True), ("127.1.2.3", True), ("localhost", True), ("LOCALHOST.", True),
    ("::1", True), ("[::1]", True), ("10.0.0.1", False), ("example.com", False),
    ("localhost.example.com", False), ("evil.localhost", False), ("", False), (None, False),
])
def test_is_loopback_host(host, local):
    assert loopback.is_loopback_host(host) is local


def test_agent_local_webhook_bypasses_proxy(proxy, ollama):
    url = f"http://127.0.0.1:{ollama.server_port}/hook"
    result = agent_webhook.post(url, {"text": "x"}, token="secret")
    assert result["ok"] and result["status"] == 200
    assert ollama.hits == [("POST", "/hook", "Bearer secret")]
    assert proxy.hits == []


@pytest.mark.parametrize("url", [
    "http://example.com/hook", "http://8.8.8.8/hook", "http://[2001:4860::8888]/hook",
    "http://nas.example.org:5678/hook",
])
def test_agent_token_never_over_plain_http_to_public_host(monkeypatch, url):
    def fail(*args, **kwargs):
        raise AssertionError("must not connect")
    monkeypatch.setattr(agent_webhook._opener, "open", fail)
    monkeypatch.setattr(agent_webhook._local_opener, "open", fail)
    result = agent_webhook.post(url, {"text": "x"}, token="secret")
    assert result == {"ok": False, "status": None, "message": "", "error": "insecure_url"}


@pytest.mark.parametrize("url", [
    "http://192.168.1.20:5678/webhook/x", "http://10.0.0.5/hook", "http://172.20.3.4/hook",
    "http://169.254.10.1/hook", "http://[fd00::1]/hook", "http://[fe80::1]/hook",
    "http://nas.local:5678/hook", "http://nas/hook", "http://NAS.LOCAL./hook",
])
def test_agent_token_over_plain_http_to_local_network(monkeypatch, url):
    seen = []

    def fake_open(request, timeout):
        seen.append(request.get_header("Authorization"))
        raise agent_webhook.urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr(agent_webhook._opener, "open", fake_open)
    result = agent_webhook.post(url, {"text": "x"}, token="secret")
    assert result["error"] == "unreachable"
    assert seen == ["Bearer secret"]


def test_agent_plain_http_without_token_still_allowed(monkeypatch):
    seen = []

    def fake_open(request, timeout):
        seen.append(request)
        raise agent_webhook.urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr(agent_webhook._opener, "open", fake_open)
    result = agent_webhook.post("http://example.com/hook", {"text": "x"})
    assert result["error"] == "unreachable"
    assert len(seen) == 1 and not seen[0].has_header("Authorization")


def test_agent_https_with_token_uses_normal_opener(monkeypatch):
    seen = []

    def fake_open(request, timeout):
        seen.append(request.get_header("Authorization"))
        raise agent_webhook.urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr(agent_webhook._opener, "open", fake_open)
    agent_webhook.post("https://example.com/hook", {"text": "x"}, token="secret")
    assert seen == ["Bearer secret"]
