"""
NoType – Ollama Lifecycle Management
Downloads a PORTABLE Ollama into %APPDATA%/NoType/ollama (no admin installer),
runs `ollama serve` as a child process, and pulls the cleanup LLM – all
in-app, so the user never has to leave NoType to enable AI cleanup.

If the user already has a system-wide Ollama running (service on :11434),
we simply use it and never manage a portable copy.
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile

from config import CONFIG_DIR, get_config

logger = logging.getLogger("NoType.Ollama")


def _t(de: str, en: str) -> str:
    """User-facing progress strings follow the app language."""
    try:
        return en if get_config().get("app_language") == "en" else de
    except Exception:
        return de

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_DIR = os.path.join(CONFIG_DIR, "ollama")
OLLAMA_EXE = os.path.join(OLLAMA_DIR, "ollama.exe")
# Models live inside our own dir, so the uninstaller removes them together
# with the rest of %APPDATA%\NoType (nsis.deleteAppDataOnUninstall).
OLLAMA_MODELS_DIR = os.path.join(OLLAMA_DIR, "models")
ZIP_NAME = "ollama-windows-amd64.zip"
DOWNLOAD_URL = f"https://github.com/ollama/ollama/releases/latest/download/{ZIP_NAME}"
# "latest" = newest stable release; GitHub excludes pre-releases here.
RELEASES_API = "https://api.github.com/repos/ollama/ollama/releases/latest"

_serve_proc = None
_state_lock = threading.Lock()
_job_handle = None
# True while update() swaps the program files – start() must not spawn the
# half-replaced exe in the meantime (e.g. AI mode toggled during an update).
_updating = False


# ── Kill-on-close Job Object ─────────────────────────────────────────
# Ties the spawned `ollama serve` (and its llama-server children) to THIS
# backend process: when the backend dies for ANY reason – crash, force-kill,
# task manager – Windows closes the job handle and kills the whole ollama
# tree. Without this, a force-killed app leaves an orphaned ollama behind
# (which once blocked our own build because it pinned DLLs).

class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.wintypes.DWORD),
        ("SchedulingClass", ctypes.wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def _bind_to_job(proc) -> None:
    """Assign `proc` to a kill-on-close job object owned by this process.
    Best-effort: on failure ollama simply isn't lifetime-bound (old behavior)."""
    global _job_handle
    try:
        kernel32 = ctypes.windll.kernel32
        if _job_handle is None:
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                    handle, _JobObjectExtendedLimitInformation,
                    ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(handle)
                return
            # Deliberately never closed while we live – closing it is exactly
            # what kills the job. Process exit closes it for us.
            _job_handle = handle
        if not kernel32.AssignProcessToJobObject(_job_handle, int(proc._handle)):
            logger.warning("AssignProcessToJobObject failed "
                           f"(error {ctypes.GetLastError()})")
        else:
            logger.info("ollama serve bound to kill-on-close job object")
    except Exception as e:
        logger.warning(f"Job object binding failed (non-fatal): {e}")


# ── Status probes ────────────────────────────────────────────────────

def api_running(timeout: float = 0.4) -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=timeout):
            return True
    except Exception:
        return False


def running_version(timeout: float = 0.4) -> str | None:
    """Version of the Ollama answering on :11434, None if none is up."""
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("version") or "?"
    except Exception:
        return None


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
    version = running_version()
    running = version is not None
    return {
        "installed": portable_installed() or system_installed(),
        "portable": portable_installed(),
        "running": running,
        "version": version,
        "model_pulled": model_pulled(model) if running else False,
    }


# ── Install (portable zip, no admin rights) ──────────────────────────

def _download(url: str, dest: str, report, label: str) -> None:
    """Stream `url` to `dest`, reporting 0-90 % (extraction gets the rest)."""
    req = urllib.request.Request(url, headers={"User-Agent": "NoType"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -1
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 90 / total)
                    if pct != last_pct:
                        last_pct = pct
                        report(pct, f"{label} {done // (1024*1024)} / {total // (1024*1024)} MB")


def install(on_progress=None) -> bool:
    """Download + extract the portable Ollama zip. Reports percent via
    on_progress(stage, percent, message). Returns True on success."""

    def report(pct, msg):
        if on_progress:
            on_progress("install", pct, msg)

    os.makedirs(OLLAMA_DIR, exist_ok=True)
    tmp_zip = os.path.join(OLLAMA_DIR, "ollama.zip.part")

    try:
        _download(DOWNLOAD_URL, tmp_zip, report,
                  _t("Lade Ollama herunter...", "Downloading Ollama..."))

        report(92, _t("Entpacke...", "Extracting..."))
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(OLLAMA_DIR)
        os.remove(tmp_zip)

        if not os.path.exists(OLLAMA_EXE):
            report(100, _t("Fehler: ollama.exe nicht im Archiv", "Error: ollama.exe missing from archive"))
            return False

        report(100, _t("Ollama installiert ✓", "Ollama installed ✓"))
        logger.info(f"Portable Ollama installed at {OLLAMA_DIR}")
        return True

    except Exception as e:
        logger.error(f"Ollama install failed: {e}")
        report(100, _t("Installation fehlgeschlagen", "Installation failed") + f": {e}")
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
    if _updating:
        return False

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
                _bind_to_job(_serve_proc)
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
        # Tree kill: the models run in llama-server.exe children, which a
        # plain terminate() would orphan – still holding their VRAM until
        # the backend exits.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    logger.info("Portable ollama serve stopped")


# ── Update (portable only) ───────────────────────────────────────────
# The portable copy is downloaded once and would otherwise stay on that
# version forever. A system-wide Ollama has its own updater and is never
# touched. Only runs on an explicit click – no background update checks.

def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def installed_version() -> str | None:
    """Version of OUR portable ollama.exe, read from the binary itself."""
    if not portable_installed():
        return None
    try:
        out = subprocess.run(
            [OLLAMA_EXE, "--version"], capture_output=True, text=True,
            timeout=10, cwd=OLLAMA_DIR, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # "ollama version is 0.31.1" – or, with no server up, a warning that
        # ends in "client version is 0.31.1".
        found = re.findall(r"\d+\.\d+\.\d+", out.stdout + out.stderr)
        return found[-1] if found else None
    except Exception:
        return None


def latest_version() -> str | None:
    """Newest stable Ollama release on GitHub, e.g. "0.34.4"."""
    try:
        req = urllib.request.Request(RELEASES_API, headers={
            "User-Agent": "NoType", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            tag = json.loads(resp.read().decode("utf-8")).get("tag_name", "")
        return tag.lstrip("v") or None
    except Exception as e:
        logger.warning(f"Ollama release check failed: {e}")
        return None


def _kill_portable_processes() -> None:
    """Kill every process started from OUR install dir – ollama.exe plus the
    lib/ollama/llama-server.exe model runners, including ones orphaned by an
    earlier crash – so no file stays locked and no stale model holds VRAM."""
    script = ("$dir = $env:NOTYPE_OLLAMA_DIR.TrimEnd('\\') + '\\*'; "
              "Get-CimInstance Win32_Process | "
              "Where-Object { $_.ExecutablePath -like $dir } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "NOTYPE_OLLAMA_DIR": OLLAMA_DIR},
            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        logger.warning(f"Could not stop leftover ollama processes: {e}")


def _remove(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        os.remove(path)


def _swap_in(staging: str, backup: str) -> None:
    """Move the staged release over the program files (models/ stays put).
    All-or-nothing: on any failure the previous files are moved back."""
    _remove(backup)
    os.makedirs(backup)
    moved_out, moved_in = [], []
    try:
        for name in os.listdir(staging):
            if name == "models":
                continue
            dst = os.path.join(OLLAMA_DIR, name)
            if os.path.exists(dst):
                os.replace(dst, os.path.join(backup, name))
                moved_out.append(name)
            os.replace(os.path.join(staging, name), dst)
            moved_in.append(name)
    except Exception:
        # Back into staging, not deleted – the caller may retry the swap.
        for name in moved_in:
            os.replace(os.path.join(OLLAMA_DIR, name), os.path.join(staging, name))
        for name in moved_out:
            os.replace(os.path.join(backup, name), os.path.join(OLLAMA_DIR, name))
        raise


def update(on_progress=None) -> tuple[bool, str]:
    """Bring the portable Ollama to the newest release, keeping every pulled
    model. Returns (ok, message for the UI). The old version keeps serving
    during the download; AI cleanup falls back to rules only for the few
    seconds of the swap."""
    global _updating

    def report(pct, msg):
        if on_progress:
            on_progress("update", pct, msg)

    if not portable_installed():
        return False, _t("Nur das von NoType installierte Ollama lässt sich hier aktualisieren",
                         "Only the Ollama installed by NoType can be updated here")

    report(0, _t("Suche nach Updates...", "Checking for updates..."))
    latest = latest_version()
    if not latest:
        return False, _t("Update-Prüfung fehlgeschlagen – keine Verbindung zu GitHub",
                         "Update check failed – could not reach GitHub")
    current = installed_version()
    if current and _ver(current) >= _ver(latest):
        return True, _t(f"Ollama ist aktuell ({current})", f"Ollama is up to date ({current})")

    staging = os.path.join(OLLAMA_DIR, "_update")
    backup = os.path.join(OLLAMA_DIR, "_previous")
    tmp_zip = os.path.join(OLLAMA_DIR, "ollama-update.zip.part")
    url = f"https://github.com/ollama/ollama/releases/download/v{latest}/{ZIP_NAME}"
    was_running = api_running()
    ok, message = False, ""
    try:
        _remove(staging)
        _download(url, tmp_zip, report, _t(f"Lade Ollama {latest}...", f"Downloading Ollama {latest}..."))
        report(92, _t("Entpacke...", "Extracting..."))
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(staging)
        if not os.path.exists(os.path.join(staging, "ollama.exe")):
            raise RuntimeError("ollama.exe missing from archive")

        report(96, _t("Installiere...", "Installing..."))
        _updating = True
        stop()
        _kill_portable_processes()
        for attempt in range(4):  # file handles can linger a moment after exit
            try:
                _swap_in(staging, backup)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(1.0)
        ok = True
        message = _t(f"Ollama aktualisiert: {current or '?'} → {latest} ✓",
                     f"Ollama updated: {current or '?'} → {latest} ✓")
        logger.info(f"Portable Ollama updated {current} -> {latest}")
    except PermissionError:
        message = _t("Ollama-Dateien sind noch in Benutzung – NoType neu starten und erneut versuchen",
                     "Ollama files are still in use – restart NoType and try again")
        logger.error("Ollama update: files locked, rolled back", exc_info=True)
    except Exception as e:
        message = _t("Update fehlgeschlagen", "Update failed") + f": {e}"
        logger.error(f"Ollama update failed: {e}", exc_info=True)
    finally:
        _updating = False
        for leftover in (tmp_zip, staging, backup):
            try:
                _remove(leftover)
            except Exception:
                pass

    # Old or new – bring back whatever version is in place now.
    if was_running:
        start()
    return ok, message


# ── Model pull ───────────────────────────────────────────────────────

def pull_model(model: str, on_progress=None) -> bool:
    """Pull `model` via the streaming API, forwarding percent progress."""

    def report(pct, msg):
        if on_progress:
            on_progress("pull", pct, msg)

    report(0, _t("Lade KI-Modell", "Downloading AI model") + f" '{model}'...")
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
                    report(100, _t("Fehler", "Error") + f": {info['error']}")
                    return False
                total = info.get("total") or 0
                completed = info.get("completed") or 0
                if total:
                    pct = int(completed * 100 / total)
                    if pct != last_pct:
                        last_pct = pct
                        report(pct, _t("Lade KI-Modell...", "Downloading AI model...") + f" {completed // (1024*1024)} / {total // (1024*1024)} MB")
                if info.get("status") == "success":
                    report(100, _t("KI-Modell bereit ✓", "AI model ready ✓"))
                    return True
        return model_pulled(model)
    except Exception as e:
        logger.error(f"Model pull failed: {e}")
        report(100, _t("Modell-Download fehlgeschlagen", "Model download failed") + f": {e}")
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
            on_progress("serve", 100, _t("Ollama-Server startet nicht", "Ollama server failed to start"))
        return False

    if not model_pulled(model):
        if not pull_model(model, on_progress):
            return False

    if on_progress:
        on_progress("done", 100, _t("KI-Bereinigung aktiv ✓", "AI cleanup active ✓"))
    return True


def ensure_running_if_installed() -> bool:
    """App-start helper: bring a previously installed Ollama up without
    downloading anything. Returns True when the API is reachable."""
    if api_running():
        return True
    if portable_installed() or system_installed():
        return start()
    return False
