"""settings.json on Windows: files edited by hand in another encoding, and
files a virus scanner or a sync client holds open for a moment."""

import builtins
import json
import os

import pytest

import config

SETTINGS = {"hotkey": "ctrl+shift+space", "model_size": "medium",
            "initial_prompt": "Jürgen Müller, Straße „Zitat“", "compute_type": "float16"}


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """settings.json in tmp_path; load_config() reads it fresh."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "CONFIG_FILE", str(path))
    monkeypatch.setattr(config, "_config", None)
    monkeypatch.setattr(config, "REPLACE_RETRY_DELAYS", (0, 0, 0))
    return path


def text(data=SETTINGS):
    return json.dumps(data, ensure_ascii=False, indent=2)


@pytest.mark.parametrize("raw", [
    text().encode("utf-8"),
    b"\xef\xbb\xbf" + text().encode("utf-8"),          # Notepad: UTF-8 with BOM
    text().encode("utf-16"),                            # PowerShell: > / Out-File
    b"\xfe\xff" + text().encode("utf-16-be"),
    text().encode("cp1252"),                            # Set-Content in PowerShell 5.1
], ids=["utf8", "utf8-bom", "utf16-le", "utf16-be", "cp1252"])
def test_hand_edited_encodings_are_read(settings, raw):
    settings.write_bytes(raw)
    loaded = config.load_config()
    assert loaded["model_size"] == "medium"
    assert loaded["initial_prompt"] == "Jürgen Müller, Straße „Zitat“"


def test_a_bom_does_not_bring_back_an_old_backup(settings):
    """Before: json.load() refused the BOM, the backup was promoted over the
    file the user had just edited (Electron read the edit fine)."""
    settings.write_bytes(b"\xef\xbb\xbf" + text().encode("utf-8"))
    backup = text({**SETTINGS, "model_size": "small"}).encode("utf-8")
    (settings.parent / "settings.json.bak1").write_bytes(backup)
    assert config.load_config()["model_size"] == "medium"
    assert settings.read_bytes().startswith(b"\xef\xbb\xbf")   # left as it is


def test_undecodable_text_does_not_crash_the_start(settings):
    """Before: UnicodeDecodeError was not caught, the backend died at every
    start (a crash loop the tray showed as "loading")."""
    settings.write_bytes(b"\xff\xfe\x00")   # odd-length UTF-16
    assert config.load_config()["model_size"] == config.DEFAULT_CONFIG["model_size"]


def test_broken_file_still_promotes_the_backup(settings):
    settings.write_text("{not json", encoding="utf-8")
    (settings.parent / "settings.json.bak1").write_text(text(), encoding="utf-8")
    assert config.load_config()["model_size"] == "medium"
    assert json.loads(settings.read_text(encoding="utf-8"))["model_size"] == "medium"


def locked(monkeypatch, path, times):
    """open() of `path` fails `times` times like a file a scanner holds."""
    real_open = builtins.open
    calls = []

    def fake_open(file, *args, **kwargs):
        if os.fspath(file) == str(path):
            calls.append(file)
            if len(calls) <= times:
                raise PermissionError(13, "The process cannot access the file because "
                                          "it is being used by another process")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(config, "open", fake_open, raising=False)
    return calls


def test_a_briefly_locked_file_is_read_after_a_retry(monkeypatch, settings):
    settings.write_text(text(), encoding="utf-8")
    calls = locked(monkeypatch, settings, times=2)
    assert config.load_config()["model_size"] == "medium"
    assert len(calls) == 3


def test_a_locked_file_is_not_overwritten_by_a_backup(monkeypatch, settings):
    settings.write_text(text(), encoding="utf-8")
    (settings.parent / "settings.json.bak1").write_text(
        text({**SETTINGS, "model_size": "small"}), encoding="utf-8")
    locked(monkeypatch, settings, times=99)
    assert config.load_config()["model_size"] == "small"     # for now
    assert json.loads(settings.read_text(encoding="utf-8"))["model_size"] == "medium"


def test_save_waits_for_a_locked_target(monkeypatch, settings):
    real_replace = os.replace
    tries = []

    def replace(src, dst):
        tries.append(dst)
        if len(tries) <= 2:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    config.save_config(dict(SETTINGS))
    assert len(tries) == 3
    assert json.loads(settings.read_text(encoding="utf-8"))["initial_prompt"] == SETTINGS["initial_prompt"]


def test_save_gives_up_on_a_target_that_stays_locked(monkeypatch, settings):
    tries = []

    def replace(src, dst):
        tries.append(dst)
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(PermissionError):
        config.save_config(dict(SETTINGS))
    assert len(tries) == len(config.REPLACE_RETRY_DELAYS) + 1


def test_saved_as_utf8(settings):
    config.save_config(dict(SETTINGS))
    assert "Jürgen Müller".encode("utf-8") in settings.read_bytes()
    assert config.load_config()["initial_prompt"] == SETTINGS["initial_prompt"]
