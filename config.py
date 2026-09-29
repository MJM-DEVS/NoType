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
    "app_language": "de",  # UI language: de | en
    "model_size": "small",
    "hotkey": "ctrl+shift+space",
    "mode": "hold_to_speak",  # "hold_to_speak" or "press_to_speak"
    "autostart": False,
    "auto_language_detect": True,
    "overlay_enabled": True,
    "overlay_style": "amoled",       # amoled | matrix | island | halo | buddy (retired ids are migrated by the Electron app)
    "overlay_position": "bottom",    # bottom | top | center – applies to every style
    "overlay_accent": "mint",        # mint | mono | aurora | blue | violet | red | amber
    "overlay_sounds": True,          # start/stop chimes
    "initial_prompt": "",            # custom-vocab bias for Whisper
    "dictionary": [],                # learned corrections [{"from": heard, "to": meant}]
    "learn_corrections": True,       # offer fixes typed right after a dictation
    "command_enabled": True,         # command mode: rewrite the selection by voice
    "command_hotkey": "ctrl+alt+space",
    "cleanup_mode": "fast",          # off | fast (rules) | ai (local LLM via Ollama)
    "llm_model": "qwen3:4b-instruct",  # Ollama model for cleanup_mode="ai"
    "llm_min_words": 8,              # below this, "ai" mode uses rules only (latency)
    "insert_method": "auto",         # auto (Ctrl+V, fallback typing) | type
    "live_preview_enabled": True,    # rolling transcript in the overlay while recording
    "success_toast": False,          # popup after successful dictation (errors always show)
    "gpu_safe_mode": False,          # disable HW acceleration (flaky-driver escape hatch)
    "first_run_complete": False,
}

_config = None


def load_config() -> dict:
    """Load config from file, falling back to rolling backups on corruption."""
    global _config

    def _try_read(path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "hotkey" in data:
                return data
        except (json.JSONDecodeError, IOError):
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
      1. write to settings.json.tmp + fsync
      2. rotate backups (.bak2 -> .bak3, .bak1 -> .bak2, current -> .bak1)
      3. atomic rename .tmp -> settings.json

    Even with a power-loss between steps you keep up to 3 previous-known-good
    generations under settings.json.bak1/.bak2/.bak3. The atomic rename in
    step 3 means settings.json itself is never half-written.
    """
    global _config
    _config = config

    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
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


def _try_restore_from_backup():
    """If settings.json is missing or empty, try the rolling backups."""
    for suffix in (".bak1", ".bak2", ".bak3", ".bak"):
        candidate = CONFIG_FILE + suffix
        if os.path.exists(candidate) and os.path.getsize(candidate) > 10:
            try:
                shutil.copy(candidate, CONFIG_FILE)
                return True
            except Exception:
                continue
    return False


def get_config() -> dict:
    """Get current config, loading if necessary."""
    if _config is None:
        return load_config()
    return _config


def update_config(**kwargs) -> dict:
    """Update specific config values and save."""
    config = get_config()
    config.update(kwargs)
    save_config(config)
    return config


def is_first_run() -> bool:
    """Check if this is the first time the app is launched."""
    config = get_config()
    return not config.get("first_run_complete", False)
