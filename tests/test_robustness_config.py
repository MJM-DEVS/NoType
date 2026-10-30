import os
import tempfile

# config.py creates its directory on import – never the real one.
_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

import _robustness_stubs  # noqa: E402,F401  (puts the repo on sys.path)
import config  # noqa: E402


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "CONFIG_FILE", str(path))
    monkeypatch.setattr(config, "_config", None)
    return path


def _write(path, data: dict, encoding="utf-8"):
    path.write_bytes(json.dumps(data, ensure_ascii=False).encode(encoding))


def test_bom_file_loads(cfg_file):
    _write(cfg_file, {"hotkey": "f9", "initial_prompt": "Müller"}, encoding="utf-8-sig")
    cfg = config.load_config()
    assert cfg["hotkey"] == "f9"
    assert cfg["initial_prompt"] == "Müller"


def test_non_utf8_file_falls_back_to_backup(cfg_file):
    _write(cfg_file, {"hotkey": "f9", "initial_prompt": "Müller"}, encoding="cp1252")
    _write(cfg_file.with_name("settings.json.bak1"), {"hotkey": "f8"})
    assert config.load_config()["hotkey"] == "f8"
    # the backup is promoted to the main file
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["hotkey"] == "f8"


def test_non_utf8_file_without_backup_gives_defaults(cfg_file):
    cfg_file.write_bytes(b'{"hotkey": "\xff\xfe\x81"}')
    assert config.load_config() == config.DEFAULT_CONFIG


@pytest.mark.parametrize("content", [b"", b"{not json", b"\x00\x00\x00", b"[1, 2]"])
def test_broken_file_never_raises(cfg_file, content):
    cfg_file.write_bytes(content)
    assert config.load_config() == config.DEFAULT_CONFIG


def test_save_leaves_foreign_tmp_and_no_own_tmp(cfg_file):
    # Electron writes settings.json.tmp itself – ours must not collide.
    electron_tmp = cfg_file.with_name("settings.json.tmp")
    electron_tmp.write_text("electron", encoding="utf-8")
    config.save_config({"hotkey": "f9"})
    assert electron_tmp.read_text(encoding="utf-8") == "electron"
    assert sorted(p.name for p in cfg_file.parent.iterdir()) == [
        "settings.json", "settings.json.tmp"]


def test_concurrent_saves_stay_valid(cfg_file):
    errors = []

    def worker(i):
        try:
            for j in range(20):
                config.save_config({"hotkey": f"f{i}", "n": j})
        except Exception as e:  # pragma: no cover - the failure we look for
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["n"] == 19
    assert not [p for p in cfg_file.parent.iterdir() if p.name.endswith(".tmp")]


def test_failed_save_removes_tmp_and_raises(cfg_file, monkeypatch):
    def boom(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(config.os, "replace", boom)
    with pytest.raises(PermissionError):
        config.save_config({"hotkey": "f9"})
    assert not [p for p in cfg_file.parent.iterdir() if p.name.endswith(".tmp")]


def test_load_removes_only_stale_own_tmp_files(cfg_file):
    folder = cfg_file.parent
    stale = folder / "settings.json.abc123.tmp"   # left by a crashed save
    fresh = folder / "settings.json.def456.tmp"   # may still be written
    electron_tmp = folder / "settings.json.tmp"   # Electron's, never ours
    other = folder / "history.json.xyz.tmp"
    for p in (stale, fresh, electron_tmp, other):
        p.write_text("x", encoding="utf-8")
    old = time.time() - config._STALE_TMP_S - 60
    for p in (stale, electron_tmp, other):
        os.utime(p, (old, old))
    config.load_config()
    assert not stale.exists()
    assert fresh.exists() and electron_tmp.exists() and other.exists()
