"""The portable Ollama download is checked against the SHA-256 the release
publishes; without one it installs as before."""

import os
import sys
import tempfile

# Before config is imported (it creates its folder on import).
_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["XDG_CONFIG_HOME"] = _TMP
os.environ["APPDATA"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib  # noqa: E402
import http.server  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import threading  # noqa: E402
import zipfile  # noqa: E402

import pytest  # noqa: E402

import ollama_manager as om  # noqa: E402


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ollama.exe", b"MZ fake")
    return buf.getvalue()


ZIP = _zip_bytes()
GOOD = hashlib.sha256(ZIP).hexdigest()
BAD = "0" * 64


class _Files(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def github(tmp_path, monkeypatch):
    """A local stand-in for the release API and its downloads."""
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Files)
    server.files = {"/latest/" + om.ZIP_NAME: ZIP}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    server.base = base

    def release(digest=None, sums=None):
        asset = {"name": om.ZIP_NAME, "browser_download_url": base + "/latest/" + om.ZIP_NAME}
        if digest:
            asset["digest"] = digest
        assets = [asset]
        if sums is not None:
            server.files["/sha256sum.txt"] = sums.encode()
            assets.append({"name": "sha256sum.txt", "browser_download_url": base + "/sha256sum.txt"})
        server.files["/api"] = json.dumps({"tag_name": "v9.9.9", "assets": assets}).encode()

    server.release = release
    monkeypatch.setattr(om, "RELEASES_API", base + "/api")
    # Asset links that aren't https://github.com/ fall back to this one.
    monkeypatch.setattr(om, "DOWNLOAD_URL", base + "/latest/" + om.ZIP_NAME)
    monkeypatch.setattr(om, "OLLAMA_DIR", str(tmp_path))
    monkeypatch.setattr(om, "OLLAMA_EXE", str(tmp_path / "ollama.exe"))
    monkeypatch.setattr(om, "_t", lambda de, en: en)
    yield server
    server.shutdown()
    server.server_close()


def test_digest_match_installs(github, tmp_path):
    github.release(digest="sha256:" + GOOD.upper())
    assert om.install()
    assert (tmp_path / "ollama.exe").exists()


def test_digest_mismatch_is_rejected(github, tmp_path):
    github.release(digest="sha256:" + BAD)
    messages = []
    assert not om.install(lambda stage, pct, msg: messages.append(msg))
    assert not (tmp_path / "ollama.exe").exists()
    assert not (tmp_path / "ollama.zip.part").exists()
    assert "checksum mismatch" in messages[-1]


def test_mismatch_message_follows_app_language(github, monkeypatch):
    github.release(digest="sha256:" + BAD)
    monkeypatch.setattr(om, "_t", lambda de, en: de)
    messages = []
    assert not om.install(lambda stage, pct, msg: messages.append(msg))
    assert "Prüfsumme stimmt nicht" in messages[-1]


@pytest.mark.parametrize("asset_url,used", [
    (f"https://github.com/ollama/ollama/releases/download/v9.9.9/{om.ZIP_NAME}", "asset"),
    (f"https://example.com/{om.ZIP_NAME}", "latest"),
    (f"http://github.com/ollama/ollama/releases/download/v9.9.9/{om.ZIP_NAME}", "latest"),
])
def test_install_downloads_the_pinned_github_asset(github, monkeypatch, asset_url, used):
    github.release(digest="sha256:" + GOOD)
    api = json.loads(github.files["/api"])
    api["assets"][0]["browser_download_url"] = asset_url
    github.files["/api"] = json.dumps(api).encode()
    urls = []

    def fake_download(url, dest, report, label):
        urls.append(url)
        with open(dest, "wb") as f:
            f.write(ZIP)
        return GOOD
    monkeypatch.setattr(om, "_download", fake_download)
    assert om.install()
    assert urls == [asset_url if used == "asset" else om.DOWNLOAD_URL]


def test_sha256sum_txt_is_used(github, tmp_path):
    github.release(sums=f"{BAD}  ./other.zip\n{GOOD}  ./{om.ZIP_NAME}\n")
    assert om.install()
    github.release(sums=f"{BAD} *{om.ZIP_NAME}\n")
    (tmp_path / "ollama.exe").unlink()
    assert not om.install()


def test_no_checksum_installs_as_before(github, tmp_path, caplog):
    github.release()
    with caplog.at_level(logging.INFO, logger="NoType.Ollama"):
        assert om.install()
    assert "not verified" in caplog.text


def test_api_down_installs_as_before(github, tmp_path, caplog):
    # No /api file: the release check fails, the plain link still works.
    with caplog.at_level(logging.INFO, logger="NoType.Ollama"):
        assert om.install()
    assert "not verified" in caplog.text


def test_update_rejects_mismatch_before_touching_files(github, tmp_path, monkeypatch):
    (tmp_path / "ollama.exe").write_bytes(b"old")
    github.release(digest="sha256:" + BAD)
    monkeypatch.setattr(om, "installed_version", lambda: "0.1.0")
    monkeypatch.setattr(om, "api_running", lambda timeout=0.4: False)

    def fake_download(url, dest, report, label):
        assert url.endswith(f"/v9.9.9/{om.ZIP_NAME}")
        with open(dest, "wb") as f:
            f.write(ZIP)
        return GOOD
    monkeypatch.setattr(om, "_download", fake_download)
    monkeypatch.setattr(om, "stop", lambda: pytest.fail("must not stop for a bad download"))
    ok, message = om.update()
    assert not ok and "checksum mismatch" in message
    assert (tmp_path / "ollama.exe").read_bytes() == b"old"
