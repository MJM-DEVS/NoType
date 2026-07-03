"""
NoType – Ollama Lifecycle Management
Downloads a PORTABLE Ollama into %APPDATA%/NoType/ollama (no admin installer),
runs `ollama serve` as a child process, and pulls the cleanup LLM – all
in-app, so the user never has to leave NoType to enable AI cleanup.

If the user already has a system-wide Ollama running (service on :11434),
we simply use it and never manage a portable copy.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile

from config import CONFIG_DIR

logger = logging.getLogger("NoType.Ollama")

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_DIR = os.path.join(CONFIG_DIR, "ollama")
OLLAMA_EXE = os.path.join(OLLAMA_DIR, "ollama.exe")
# Models live inside our own dir so uninstalling NoType removes everything.
OLLAMA_MODELS_DIR = os.path.join(OLLAMA_DIR, "models")
DOWNLOAD_URL = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"

_serve_proc = None
_state_lock = threading.Lock()


# ── Status probes ────────────────────────────────────────────────────

def api_running(timeout: float = 0.4) -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=timeout):
            return True
    except Exception:
        return False


def portable_installed() -> bool:
    return os.path.exists(OLLAMA_EXE)


def system_installed() -> bool:
    return shutil.which("ollama") is not None


def model_pulled(model: str) -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=1.0) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name", "") for m in tags.get("models", [])]
        return any(n == model or n.startswith(model) for n in names)
    except Exception:
        return False


def status(model: str) -> dict:
    running = api_running()
    return {
        "installed": portable_installed() or system_installed(),
        "portable": portable_installed(),
        "running": running,
        "model_pulled": model_pulled(model) if running else False,
    }


# ── Install (portable zip, no admin rights) ──────────────────────────

def install(on_progress=None) -> bool:
    """Download + extract the portable Ollama zip. Reports percent via
    on_progress(stage, percent, message). Returns True on success."""

    def report(pct, msg):
        if on_progress:
            on_progress("install", pct, msg)

    os.makedirs(OLLAMA_DIR, exist_ok=True)
    tmp_zip = os.path.join(OLLAMA_DIR, "ollama.zip.part")

    try:
        report(0, "Lade Ollama herunter...")
        req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "NoType"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_pct = -1
            with open(tmp_zip, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done * 90 / total)  # download = 0-90%
                        if pct != last_pct:
                            last_pct = pct
                            report(pct, f"Lade Ollama herunter... {done // (1024*1024)} MB")

        report(92, "Entpacke...")
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(OLLAMA_DIR)
        os.remove(tmp_zip)

        if not os.path.exists(OLLAMA_EXE):
            report(100, "Fehler: ollama.exe nicht im Archiv")
            return False

        report(100, "Ollama installiert ✓")
        logger.info(f"Portable Ollama installed at {OLLAMA_DIR}")
        return True

    except Exception as e:
        logger.error(f"Ollama install failed: {e}")
        report(100, f"Installation fehlgeschlagen: {e}")
        try:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
        except Exception:
            pass
        return False


# ── Serve process management ─────────────────────────────────────────

def start() -> bool:
    """Ensure an Ollama API is reachable. Prefers an already-running
    (system) instance; otherwise spawns our portable `ollama serve`."""
    global _serve_proc
    if api_running():
        return True

    exe = OLLAMA_EXE if portable_installed() else shutil.which("ollama")
    if not exe:
        return False

    with _state_lock:
        if _serve_proc is not None and _serve_proc.poll() is None:
            pass  # already spawned, just not up yet
        else:
            env = os.environ.copy()
            if exe == OLLAMA_EXE:
                os.makedirs(OLLAMA_MODELS_DIR, exist_ok=True)
                env["OLLAMA_MODELS"] = OLLAMA_MODELS_DIR
            try:
                _serve_proc = subprocess.Popen(
                    [exe, "serve"],
                    env=env,
                    # Own cwd – NEVER inherit the backend's. An inherited cwd
                    # pins our install folder (locks DLL deletion → breaks
                    # rebuilds/updates while ollama runs).
                    cwd=OLLAMA_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                logger.info(f"Spawned ollama serve (pid={_serve_proc.pid})")
            except Exception as e:
                logger.error(f"Failed to spawn ollama serve: {e}")
                return False

    # Wait for the API to come up (GPU runtime init can take a few seconds).
    for _ in range(40):
        if api_running():
            return True
        time.sleep(0.5)
    logger.warning("ollama serve did not become ready within 20s")
    return False


def stop() -> None:
    """Stop OUR portable serve process (frees its RAM/VRAM). A system-wide
    Ollama service is left untouched."""
    global _serve_proc
    with _state_lock:
        proc, _serve_proc = _serve_proc, None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    logger.info("Portable ollama serve stopped")


# ── Model pull ───────────────────────────────────────────────────────

def pull_model(model: str, on_progress=None) -> bool:
    """Pull `model` via the streaming API, forwarding percent progress."""

    def report(pct, msg):
        if on_progress:
            on_progress("pull", pct, msg)

    report(0, f"Lade KI-Modell '{model}'...")
    payload = json.dumps({"name": model, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL + "/api/pull", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            last_pct = -1
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                info = json.loads(line)
                if info.get("error"):
                    report(100, f"Fehler: {info['error']}")
                    return False
                total = info.get("total") or 0
                completed = info.get("completed") or 0
                if total:
                    pct = int(completed * 100 / total)
                    if pct != last_pct:
                        last_pct = pct
                        report(pct, f"Lade KI-Modell... {completed // (1024*1024)} / {total // (1024*1024)} MB")
                if info.get("status") == "success":
                    report(100, "KI-Modell bereit ✓")
                    return True
        return model_pulled(model)
    except Exception as e:
        logger.error(f"Model pull failed: {e}")
        report(100, f"Modell-Download fehlgeschlagen: {e}")
        return False


# ── One-call orchestration ───────────────────────────────────────────

def setup(model: str, on_progress=None) -> bool:
    """Full path from nothing to ready: install (if needed) → serve →
    pull model (if needed). Idempotent – safe to call when parts exist."""
    if not (portable_installed() or system_installed()):
        if not install(on_progress):
            return False

    if not start():
        if on_progress:
            on_progress("serve", 100, "Ollama-Server startet nicht")
        return False

    if not model_pulled(model):
        if not pull_model(model, on_progress):
            return False

    if on_progress:
        on_progress("done", 100, "KI-Bereinigung aktiv ✓")
    return True


def ensure_running_if_installed() -> bool:
    """App-start helper: bring a previously installed Ollama up without
    downloading anything. Returns True when the API is reachable."""
    if api_running():
        return True
    if portable_installed() or system_installed():
        return start()
    return False
