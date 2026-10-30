"""
NoType – Configuration Management
Loads and saves user settings from/to %APPDATA%/NoType/settings.json (Windows)
or ~/.config/notype/settings.json elsewhere. Falls back to the script directory
in dev/portable mode.

The user-data directory matches what Electron writes via `app.getPath('userData')`,
so backend and frontend share one settings file.
"""

import json
import os
import shutil
import tempfile
import threading
import time

APP_NAME = "NoType"


def _user_data_dir() -> str:
    """%APPDATA%/NoType on Windows, ~/.config/NoType elsewhere."""
    appdata = os.environ.get("APPDATA")  # Windows
    if appdata:
        return os.path.join(appdata, APP_NAME)
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, APP_NAME)


CONFIG_DIR = _user_data_dir()
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")


def _migrate_from_legacy_path() -> None:
    """One-shot: copy a pre-existing settings.json from older locations
    (script directory, PyInstaller _internal/) into the new user-data dir."""
    if os.path.exists(CONFIG_FILE):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "settings.json"),
        os.path.join(os.path.dirname(here), "settings.json"),  # one level up
    ]
    for src in candidates:
        if os.path.exists(src):
            try:
                shutil.copy(src, CONFIG_FILE)
                return
            except Exception:
                pass


_migrate_from_legacy_path()

DEFAULT_CONFIG = {
    "language": "de",  # any Whisper language code – the UI offers all 99
    # "languages": [{"code": "de", "share": 80}, …] – the languages the user
    # speaks; not defaulted, a config without it reads as `language` at 100 %
    "app_language": "de",  # UI language: de | en
    "model_size": "small",
    "hotkey": "ctrl+shift+space",
    "mode": "hold_to_speak",  # "hold_to_speak" or "press_to_speak"
    "autostart": False,
    "update_check": True,            # ask GitHub for a newer release (Electron side)
    "auto_language_detect": True,    # also recognize languages outside `languages`
    "spoken_punctuation": True,      # "Fragezeichen", "neue Zeile" … become characters
    "overlay_enabled": True,
    "overlay_style": "amoled",       # amoled | matrix | island | halo | buddy | slime | dragon | mimic | line (retired ids are migrated by the Electron app)
    "overlay_position": "bottom",    # bottom | top | center – applies to every style
    "overlay_accent": "mint",        # mint | mono | aurora | blue | violet | red | amber
    "overlay_sounds": True,          # start/stop chimes
    "initial_prompt": "",            # custom-vocab bias for Whisper
    "dictionary": [],                # learned corrections [{"from": heard, "to": meant}]
    "learn_corrections": True,       # offer fixes typed right after a dictation
    "save_history": True,            # keep the last 10 dictations in history.json (Electron side)
    "command_enabled": True,         # command mode: rewrite the selection by voice
    "command_hotkey": "ctrl+alt+space",
    "agent_enabled": False,          # agent hotkey: POST the dictation to agent_url instead of typing it
    "agent_hotkey": "ctrl+shift+alt+space",
    "agent_url": "",
    "agent_token": "",               # sent as "Authorization: Bearer …" when set
    "agent_send_context": True,      # include app name + window title in the request
    "cleanup_mode": "fast",          # off | fast (rules) | ai (local LLM via Ollama)
    "llm_model": "qwen3:4b-instruct",  # Ollama model for cleanup_mode="ai"
    "llm_min_words": 8,              # below this, "ai" mode uses rules only (latency)
    "insert_method": "auto",         # auto (Ctrl+V, fallback typing) | type
    "mic_device_name": None,         # input device by name, None = system default (replaces mic_device_index)
    "mute_while_dictating": False,   # mute the speakers while recording
    "ai_context": True,              # copies around a dictation into an AI app/terminal go along
    "live_preview_enabled": True,    # rolling transcript in the overlay while recording
    "success_toast": False,          # popup after successful dictation (errors always show)
    "gpu_safe_mode": False,          # disable HW acceleration (flaky-driver escape hatch)
    "first_run_complete": False,
}

_config = None
# Several backend threads save (selftest, transcription, Ollama setup, IPC).
_save_lock = threading.Lock()


# A temp file of save_config() older than this was left by a crash or power
# loss; a younger one may still be written by another backend instance.
_STALE_TMP_S = 600


def _remove_stale_tmp_files() -> None:
    """Delete settings.json.<random>.tmp files a crashed save left behind.
    Only our own unique names: Electron's settings.json.tmp is never touched."""
    folder = os.path.dirname(CONFIG_FILE)
    prefix = os.path.basename(CONFIG_FILE) + "."
    electron_tmp = prefix + "tmp"
    try:
        names = os.listdir(folder)
    except OSError:
        return
    with _save_lock:
        for name in names:
            if (not (name.startswith(prefix) and name.endswith(".tmp"))
                    or name == electron_tmp):
                continue
            path = os.path.join(folder, name)
            try:
                if time.time() - os.path.getmtime(path) > _STALE_TMP_S:
                    os.remove(path)
            except OSError:
                pass


def load_config() -> dict:
    """Load config from file, falling back to rolling backups on corruption."""
    global _config
    _remove_stale_tmp_files()

    def _try_read(path: str):
        try:
            # utf-8-sig: a file re-saved by Notepad & co. may start with a BOM.
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict) and "hotkey" in data:
                return data
        except (ValueError, OSError):
            # ValueError covers JSONDecodeError and UnicodeDecodeError (a
            # file saved in a non-UTF-8 encoding) – try the next candidate.
            return None
        return None

    candidates = [
        CONFIG_FILE,
        CONFIG_FILE + ".bak1",
        CONFIG_FILE + ".bak2",
        CONFIG_FILE + ".bak3",
        CONFIG_FILE + ".bak",   # legacy single-backup name
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        data = _try_read(path)
        if data is not None:
            if path != CONFIG_FILE:
                # main file was bad – promote the recovered backup
                try:
                    shutil.copy(path, CONFIG_FILE)
                except Exception:
                    pass
            _config = {**DEFAULT_CONFIG, **data}
            return _config

    _config = DEFAULT_CONFIG.copy()
    return _config


def save_config(config: dict) -> None:
    """Save config dict to settings.json atomically with 3 rolling backups.

    Sequence:
      1. write to a unique settings.json.*.tmp + fsync
      2. rotate backups (.bak2 -> .bak3, .bak1 -> .bak2, current -> .bak1)
      3. atomic rename .tmp -> settings.json

    Even with a power-loss between steps you keep up to 3 previous-known-good
    generations under settings.json.bak1/.bak2/.bak3. The atomic rename in
    step 3 means settings.json itself is never half-written.

    Serialised by a module lock; the temp name is unique because Electron
    writes settings.json.tmp itself. Raises on failure (callers decide).
    """
    global _config
    _config = config

    with _save_lock:
        fd, tmp = tempfile.mkstemp(prefix="settings.json.", suffix=".tmp",
                                   dir=os.path.dirname(CONFIG_FILE))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass

            # Rotate backups: .bak2 -> .bak3, .bak1 -> .bak2, current -> .bak1
            try:
                bak1 = CONFIG_FILE + ".bak1"
                bak2 = CONFIG_FILE + ".bak2"
                bak3 = CONFIG_FILE + ".bak3"
                if os.path.exists(bak2):
                    shutil.copy(bak2, bak3)
                if os.path.exists(bak1):
                    shutil.copy(bak1, bak2)
                if os.path.exists(CONFIG_FILE):
                    shutil.copy(CONFIG_FILE, bak1)
            except Exception:
                pass  # backup failure must not block the actual save

            os.replace(tmp, CONFIG_FILE)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def get_config() -> dict:
    """Get current config, loading if necessary."""
    if _config is None:
        return load_config()
    return _config
