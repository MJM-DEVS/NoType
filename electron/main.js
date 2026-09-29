const { app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, nativeImage, screen, nativeTheme } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const fs = require('fs');

// ── Single-Instance Lock ──
// Prevents double hotkey registration when boot-autostart and a manual launch collide.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

// ── GPU Safe Mode (escape hatch for flaky drivers) ──────────────────
// MUST run at module load, BEFORE app.whenReady() / the GPU process starting –
// disableHardwareAcceleration / command-line switches are no-ops afterwards.
// When the user's GPU driver crashes with screen artifacts (TDR), enabling
// `gpu_safe_mode` moves compositing to the CPU and stops provoking the driver.
// We read settings.json synchronously here; app.getPath('userData') is already
// resolvable at module load (only the cached USER_DATA_DIR is deferred).
try {
  const _cfgPath = path.join(app.getPath('userData'), 'settings.json');
  const _cfg = fs.existsSync(_cfgPath) ? JSON.parse(fs.readFileSync(_cfgPath, 'utf-8')) : {};
  if (_cfg.gpu_safe_mode) {
    app.disableHardwareAcceleration();
    console.log('GPU safe mode ON – hardware acceleration disabled');
  }
} catch (_) { /* settings unreadable – default to normal GPU path */ }

// ── Paths ──
// In a packaged build (electron-builder), Python sources live under resourcesPath.
const IS_PACKAGED = app.isPackaged;
const ROOT_DIR = IS_PACKAGED ? process.resourcesPath : path.join(__dirname, '..');
const PYTHON_VENV = path.join(ROOT_DIR, 'venv', 'Scripts', 'python.exe');
const PYTHON_FALLBACK = path.join(ROOT_DIR, 'venv', 'Scripts', 'pythonw.exe');
const BACKEND_SCRIPT = path.join(ROOT_DIR, 'backend.py');
// PyInstaller-frozen backend (preferred in packaged builds)
const BACKEND_EXE = path.join(ROOT_DIR, 'backend.exe');
// PyInstaller --onedir output: dist/backend/backend.exe (preferred in packaged builds).
// Try both the onedir layout (resources/backend/backend.exe) and onefile (resources/backend.exe).
const BACKEND_EXE_DIR = path.join(ROOT_DIR, 'backend', 'backend.exe');
// User-data dir (matches what config.py writes): %APPDATA%/NoType on Windows.
// We call app.getPath('userData') lazily inside whenReady — at module load time
// the userData path is not yet resolved.
let USER_DATA_DIR;
let CONFIG_FILE;
let HISTORY_FILE;
const PRELOAD = path.join(__dirname, 'preload.js');

function initPaths() {
  USER_DATA_DIR = app.getPath('userData');
  CONFIG_FILE = path.join(USER_DATA_DIR, 'settings.json');
  HISTORY_FILE = path.join(USER_DATA_DIR, 'history.json');
  // One-shot migration: if no settings.json in userData yet, pull one from the
  // project root / packaged resources so existing users keep their config.
  if (!fs.existsSync(CONFIG_FILE)) {
    const candidates = [
      path.join(ROOT_DIR, 'settings.json'),
      path.join(__dirname, '..', 'settings.json'),
    ];
    for (const src of candidates) {
      if (fs.existsSync(src)) {
        try { fs.mkdirSync(USER_DATA_DIR, { recursive: true }); fs.copyFileSync(src, CONFIG_FILE); break; } catch (_) {}
      }
    }
  }
}

// ── History & Stats Persistence ──────────────────────────────────────
// Stored shape:
//   {
//     "history": [ { text, time }, ... up to MAX_HISTORY ],
//     "stats":   { "Mon May 14 2026": { count, words }, ... }
//   }
// Written atomically (tmp + rename) on every change. Robust to corrupt files:
// if the JSON can't be parsed, we start fresh and keep going.
function todayKey() { return new Date().toDateString(); }

function loadHistory() {
  try {
    if (fs.existsSync(HISTORY_FILE)) {
      const raw = fs.readFileSync(HISTORY_FILE, 'utf-8');
      const data = JSON.parse(raw);
      if (Array.isArray(data.history)) {
        clipboardHistory = data.history.slice(0, MAX_HISTORY);
      }
      if (data.stats && typeof data.stats === 'object') {
        statsByDate = data.stats;
        const tk = todayKey();
        todayStats = statsByDate[tk] || { date: tk, count: 0, words: 0 };
        todayStats.date = tk;  // ensure shape
      }
    }
  } catch (e) {
    console.error('History load failed (starting fresh):', e.message);
    clipboardHistory = [];
    statsByDate = {};
    todayStats = { date: todayKey(), count: 0, words: 0 };
  }
}

function saveHistory() {
  if (!HISTORY_FILE) return;  // initPaths() not yet called
  try {
    // Roll today's stats into the persisted map before writing.
    statsByDate[todayStats.date] = { count: todayStats.count, words: todayStats.words };
    // Prune statsByDate to last 90 days to keep file small.
    const cutoff = Date.now() - 90 * 24 * 3600 * 1000;
    for (const key of Object.keys(statsByDate)) {
      const t = Date.parse(key);
      if (!isNaN(t) && t < cutoff) delete statsByDate[key];
    }
    const data = { history: clipboardHistory, stats: statsByDate };
    const tmp = HISTORY_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf-8');
    fs.renameSync(tmp, HISTORY_FILE);
  } catch (e) {
    console.error('History save failed:', e.message);
  }
}

// Debounced save: each transcription calls both addToHistory and trackStats,
// i.e. two writes back-to-back. Coalesce them into one write ~1s later, off
// the post-transcription hot path. quitApp() flushes synchronously.
let _saveTimer = null;
function scheduleSave() {
  if (_saveTimer) return;
  _saveTimer = setTimeout(() => { _saveTimer = null; saveHistory(); }, 1000);
}

// ── State ──
let tray = null;
let overlayWin = null;
let overlayReady = false;          // overlay webContents finished loading
let overlayIdleTimer = null;       // destroys the hidden overlay after long idle
const OVERLAY_IDLE_MS = 5 * 60 * 1000;
let settingsWin = null;
let toastWin = null;
let suggestWin = null;           // "learn this correction?" pill
let suggestItems = [];
let suggestTimer = null;
let pythonProcess = null;
let pythonStartCount = 0;          // how many times we've spawned the backend
let lastBackendDeath = 0;          // ms timestamp of the last unexpected exit
let consecutiveFastCrashes = 0;    // crashes <10s apart in a row (circuit breaker)
let crashToastShown = false;       // rate-limit the crash toast during a storm
let backendUptimeTimer = null;     // resets the breaker after sustained uptime
let backendGaveUp = false;         // true once we stop auto-restarting
let quitRequested = false;         // set by quitApp() to suppress restart
let isRecording = false;
let isPaused = false;
let isTranscribing = false;
let modelLoaded = false;
let modelInfo = null;
let pendingModelSwitch = null;   // model id the user just switched to → "ready" toast
let bootReadyToastShown = false; // one "NoType ready" toast per launch
let recordStopTs = 0;            // when the user released – for the insert-latency stat
let holdReleaseViaBackend = true;// backend reports real key-up (optimistic; see 'hotkey_watch')
let onboardingWin = null;        // first-run wizard (also reachable from settings/tray)
let lastBusyTs = 0;              // throttle for the "still processing" nudge
let commandActive = false;       // current recording is a command-mode instruction
let config = {};
let clipboardHistory = [];  // Last 10 transcriptions
const MAX_HISTORY = 10;

// Stats tracking — persisted via history.json (see loadHistory/saveHistory below).
let todayStats = { date: new Date().toDateString(), count: 0, words: 0 };
let statsByDate = {};  // { "Mon May 14 2026": { count, words }, ... }

// ── Theme ──
// Returns 'dark' or 'light' based on Windows system setting (or explicit user override).
function currentTheme() {
  const override = config.overlay_theme;  // 'dark' | 'light' | 'system' (or undefined)
  if (override === 'dark' || override === 'light') return override;
  return nativeTheme.shouldUseDarkColors ? 'dark' : 'light';
}

function broadcastTheme() {
  const theme = currentTheme();
  for (const w of [settingsWin, overlayWin, toastWin, onboardingWin]) {
    if (w && !w.isDestroyed()) {
      try { w.webContents.send('set-theme', theme); } catch (_) {}
    }
  }
}

// Mica/Acrylic backdrop on Windows 11. Silently a no-op elsewhere.
// `backgroundMaterial` was added in Electron 31.4; we're on 33+.
function applyMica(win) {
  if (!win || win.isDestroyed()) return;
  try {
    if (typeof win.setBackgroundMaterial === 'function') {
      // 'mica' = subtle, 'acrylic' = stronger blur. Mica is the Win11 default for app surfaces.
      win.setBackgroundMaterial('mica');
    }
  } catch (e) {
    // Older Windows or unsupported configuration – ignore.
  }
}

// ── Config ──
function loadConfig() {
  try {
    if (fs.existsSync(CONFIG_FILE)) {
      config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf-8'));
    }
  } catch (e) {
    console.error('Config load error:', e);
  }
  config = {
    language: 'de',
    languages_enabled: ['de', 'en', 'pl', 'hr'],
    model_size: 'small',
    hotkey: 'CmdOrCtrl+Shift+Space',
    mode: 'press_to_speak',
    autostart: false,
    auto_language_detect: true,
    overlay_enabled: true,
    overlay_style: 'amoled',
    overlay_accent: 'mint',
    overlay_sounds: true,
    live_preview_enabled: true,
    success_toast: false,
    gpu_safe_mode: false,
    first_run_complete: false,
    command_enabled: true,
    command_hotkey: 'ctrl+alt+space',
    learn_corrections: true,
    ...config,
  };
  // Legacy AMOLED style ids → style + position (persist so the backend and
  // the settings page read the migrated file from now on).
  if (normalizeOverlayConfig()) writeConfigToDisk();
  return config;
}

// Atomic write of the in-memory config to settings.json (tmp + rename, with
// the same 3-generation .bak rotation config.py uses). Electron is the source
// of truth for SAVING: a dead or restarting backend can never make a save
// vanish. The backend re-reads this file on start and additionally gets
// `save_config` forwarded while it is alive.
function writeConfigToDisk() {
  if (!CONFIG_FILE) return false;
  try {
    fs.mkdirSync(path.dirname(CONFIG_FILE), { recursive: true });
    const tmp = CONFIG_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(config, null, 2), 'utf-8');
    try {
      const b1 = CONFIG_FILE + '.bak1', b2 = CONFIG_FILE + '.bak2', b3 = CONFIG_FILE + '.bak3';
      if (fs.existsSync(b2)) fs.copyFileSync(b2, b3);
      if (fs.existsSync(b1)) fs.copyFileSync(b1, b2);
      if (fs.existsSync(CONFIG_FILE)) fs.copyFileSync(CONFIG_FILE, b1);
    } catch (_) { /* backups are best-effort */ }
    fs.renameSync(tmp, CONFIG_FILE);
    return true;
  } catch (e) {
    console.error('Config write failed:', e.message);
    return false;
  }
}

// ── Python Backend ──
function startPython() {
  // Prefer the PyInstaller-frozen backend.exe (packaged build, no Python install needed).
  // Resolution: onedir → onefile → venv Python.
  let cmd, args;
  if (fs.existsSync(BACKEND_EXE_DIR)) {
    cmd = BACKEND_EXE_DIR;
    args = [];
  } else if (fs.existsSync(BACKEND_EXE)) {
    cmd = BACKEND_EXE;
    args = [];
  } else {
    cmd = fs.existsSync(PYTHON_VENV) ? PYTHON_VENV : PYTHON_FALLBACK;
    args = [BACKEND_SCRIPT];
  }
  console.log(`Starting backend: ${cmd} ${args.join(' ')}`);

  pythonProcess = spawn(cmd, args, {
    cwd: ROOT_DIR,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });

  let buffer = '';
  pythonProcess.stdout.on('data', (data) => {
    buffer += data.toString();
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (line.trim()) handlePythonMessage(line.trim());
    }
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error('Python:', data.toString().trim());
  });

  pythonProcess.on('close', (code) => {
    console.log(`Python exited (code ${code})`);
    pythonProcess = null;
    modelLoaded = false;
    if (backendUptimeTimer) { clearTimeout(backendUptimeTimer); backendUptimeTimer = null; }

    // If we asked it to quit, don't relaunch.
    if (quitRequested) return;

    // If any recording was in flight, reset the state machine so the hotkey
    // doesn't end up locked.
    if (isRecording || isTranscribing) finishTranscription();

    // ── Circuit breaker ──
    // A deterministic native crash (e.g. a CUDA/ctranslate2 segfault from a
    // GPU driver reset) would otherwise respawn forever, and EACH respawn
    // re-initialises a fresh CUDA context that prevents a wedged driver from
    // settling. Count crashes that happen <10s apart; after 5 in a row, STOP
    // and require a manual restart. A single watchdog-kill (uptime >65s) lands
    // in the `else` branch and resets the counter, so it's never mistaken for
    // a crash loop.
    const now = Date.now();
    if (now - lastBackendDeath < 10_000) consecutiveFastCrashes++;
    else consecutiveFastCrashes = 0;
    lastBackendDeath = now;

    if (consecutiveFastCrashes >= 5) {
      backendGaveUp = true;
      console.error('Backend crash-looping – giving up auto-restart');
      if (!crashToastShown) {
        showToast(trayT().backendDead, 'error');
        crashToastShown = true;
      }
      updateTray('loading');
      return;  // require manual restart – stop hammering the GPU
    }

    // Exponential backoff: 2s, 4s, 8s, 16s, 32s (capped at 60s).
    const delay = Math.min(60_000, 2_000 * 2 ** consecutiveFastCrashes);
    if (!crashToastShown) {
      showToast(trayT().backendCrashed, 'error');
      crashToastShown = true;
    }
    console.log(`Restarting backend in ${delay} ms (fast-crash streak ${consecutiveFastCrashes})`);
    setTimeout(() => { if (!quitRequested && !backendGaveUp) startPython(); }, delay);
  });

  pythonProcess.on('error', (err) => {
    console.error('Backend spawn error:', err);
  });

  // Reset the breaker once the backend has run cleanly for 30s – a transient
  // one-off crash shouldn't count toward the give-up threshold forever.
  backendUptimeTimer = setTimeout(() => {
    if (pythonProcess) { consecutiveFastCrashes = 0; crashToastShown = false; }
  }, 30_000);

  pythonStartCount++;
}

// ── IPC Watchdog ────────────────────────────────────────────────────
// If the backend stops responding (frozen Whisper call, IPC pipe stuck) we'd
// otherwise sit there with an unusable hotkey. Every minute we send a ping and
// expect a pong back within 5 s. Two missed pings = kill + auto-restart.
let lastPongTs = Date.now();
let missedPings = 0;
function startBackendWatchdog() {
  setInterval(() => {
    if (!pythonProcess || quitRequested) return;
    // Don't ping during a recording – the backend may legitimately be busy
    // running Whisper and not service IPC for a few seconds.
    if (isRecording || isTranscribing) return;

    const sinceLastPong = Date.now() - lastPongTs;
    if (sinceLastPong > 65_000) {
      missedPings++;
      console.error(`Backend missed ping (#${missedPings}, ${sinceLastPong}ms since last pong)`);
      if (missedPings >= 2) {
        console.error('Backend wedged – killing for auto-restart');
        try { pythonProcess.kill('SIGKILL'); } catch (_) {}
        missedPings = 0;
        // pythonProcess.on('close', ...) will trigger the restart.
        return;
      }
    }
    try { sendToPython('ping'); } catch (_) {}
  }, 60_000);
}

function sendToPython(command, data = null) {
  if (!pythonProcess || !pythonProcess.stdin.writable) return;
  const msg = { command };
  if (data) msg.data = data;
  try {
    pythonProcess.stdin.write(JSON.stringify(msg) + '\n');
  } catch (e) {
    console.error('Failed to send to Python:', e);
  }
}

function handlePythonMessage(line) {
  try {
    const msg = JSON.parse(line);
    const { type, data } = msg;

    // Any reply – including arbitrary status updates – is proof the backend
    // is alive. Reset the watchdog counters whenever we hear from it.
    lastPongTs = Date.now();
    missedPings = 0;

    switch (type) {
      case 'backend_ready':
        console.log('✓ Python backend ready');
        sendToPython('get_config');
        break;

      case 'pong':
        // Heartbeat reply – nothing else to do, lastPongTs already updated above.
        break;

      case 'hotkey_watch':
        // Backend couldn't map the hotkey → keep the short timer fallback.
        holdReleaseViaBackend = !!(data && data.ok);
        break;

      case 'hotkey_released':
        if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
        if (isRecording && (config.mode || 'press_to_speak') === 'hold_to_speak') stopRecording();
        break;

      case 'amplitude':
        if (overlayWin && !overlayWin.isDestroyed()) {
          overlayWin.webContents.send('amplitude', data.value, data.bands || null);
        }
        break;

      case 'recording_started':
        // Don't re-send to overlay – showOverlay() already did it
        isRecording = true;
        updateTray('recording');
        break;

      case 'transcribing':
        isTranscribing = true;
        updateTray('processing');
        break;

      case 'preview':
        // Rolling live transcript while recording – overlay caption only.
        if (overlayWin && !overlayWin.isDestroyed() && isRecording) {
          overlayWin.webContents.send('preview-text', data.text);
        }
        break;

      case 'dictionary_suggestions':
        if (config.learn_corrections !== false) showSuggestion(data && data.items);
        break;

      case 'transcription_done':
        finishTranscription(!!(data && data.text && !data.empty), data && data.text);
        sendToOnboarding('transcription-done', data || {});
        if (data && data.command) {
          // Command mode: the result is LLM output, not a dictation – it
          // stays out of the history (that's for learning recognition errors).
          if (data.reason === 'command_no_llm') showToast(trayT().commandNoLlm, 'warning');
          else if (data.reason === 'command_failed') showToast(trayT().commandFailed, 'error');
          else if (data.reason === 'too_short') showToast(trayT().tooShort, 'warning');
        } else if (data && data.text && !data.empty) {
          addToHistory(data.text);
          trackStats(data.text);
          // Success popup is opt-in – the inserted text IS the feedback.
          // Warnings and errors always show.
          if (config.success_toast === true) {
            // Human stats: how long from key-release to inserted text, and
            // whether the AI cleanup ran. (beam/compute live in the log.)
            showToast(data.text, 'success', {
              latency_s: recordStopTs ? Math.max(0, (Date.now() - recordStopTs) / 1000) : null,
              engine: data.cleanup || null,
            });
          }
        } else if (data && data.reason === 'too_short') {
          showToast(trayT().tooShort, 'warning');
        }
        break;

      case 'recording_paused':
        isPaused = true;
        if (overlayWin && !overlayWin.isDestroyed()) {
          overlayWin.webContents.send('recording-paused');
        }
        updateTray('paused');
        break;

      case 'recording_resumed':
        isPaused = false;
        if (overlayWin && !overlayWin.isDestroyed()) {
          overlayWin.webContents.send('recording-resumed');
        }
        updateTray('recording');
        break;

      case 'model_ready':
        modelLoaded = true;
        modelInfo = data;
        console.log(`✓ Model: ${data.model} on ${data.device}`);
        updateTray('ready');
        if (pendingModelSwitch) {
          showToast(trayT().modelReady.replace('{model}', modelLabel(data.model)), 'success');
          pendingModelSwitch = null;
        } else if (!bootReadyToastShown) {
          // Once per launch – answers "is it ready yet?" on slower machines.
          const dev = String(data.device || '').split(' (')[0];
          showToast(trayT().bootReady.replace('{model}', modelLabel(data.model)).replace('{device}', dev), 'success');
        }
        bootReadyToastShown = true;
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('model-ready', data);
        }
        sendToOnboarding('model-ready', data);
        break;

      case 'config':
        // If the backend's hotkey/mode differ from what we registered at boot, re-register.
        {
          const prevHotkey = config.hotkey, prevMode = config.mode;
          const prevCmd = `${config.command_enabled}|${config.command_hotkey}`;
          config = data;
          if (normalizeOverlayConfig()) writeConfigToDisk();
          if (data.hotkey !== prevHotkey || data.mode !== prevMode ||
              `${data.command_enabled}|${data.command_hotkey}` !== prevCmd) {
            registerHotkeys();
          }
        }
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('config', data);
        }
        break;

      case 'config_saved':
        config = data;
        normalizeOverlayConfig();
        registerHotkeys();
        break;

      case 'status':
        console.log('Status:', data.message);
        sendToOnboarding('status', data && data.message);
        break;

      case 'devices':
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('devices', data.devices);
        }
        sendToOnboarding('devices', data.devices);
        break;

      case 'models':
        // Catalog + downloaded state + hardware for the model cards.
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('models', data);
        }
        sendToOnboarding('models', data);
        break;

      case 'ollama_status':
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('ollama-status', data);
        }
        break;

      case 'ollama_progress':
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('ollama-progress', data);
        }
        break;

      case 'error':
        console.error('Backend error:', data.message);
        // CRITICAL: if an error arrives while we were mid-transcription,
        // restore the state machine so the hotkey works again. Without this
        // a single transcription error locks the app dead.
        if (isRecording || isTranscribing) {
          finishTranscription();
        }
        showToast(data.message, 'error');
        break;
    }
  } catch (e) {
    // Not JSON – just log it
    console.log('Python output:', line);
  }
}

// ── Overlay Window ──
// Each visual style has its own size and on-screen anchor. Anchors are
// computed per-display so multi-monitor setups work.
// Every style can sit at the bottom, top or center of the screen
// (config.overlay_position). `bottom`/`top` are the per-style margins from
// that screen edge. All styles render the live transcript themselves; each
// window carries transparent padding so glows fade out inside it instead of
// being clipped into a hard rectangle (sizes match the fixed layouts in
// overlay.html).
const OVERLAY_STYLES = {
  amoled: { w: 708, h: 92,  bottom: 14, top: 2 },   // pill 680×64
  matrix: { w: 708, h: 104, bottom: 14, top: 2 },   // card 680×76
  island: { w: 648, h: 116, bottom: 6,  top: 0 },   // morphing shape, ≤600×72
  halo:   { w: 616, h: 118, bottom: 0,  top: 0 },   // capsule 560×58 + rim glow
  buddy:  { w: 652, h: 108, bottom: 10, top: 0 },   // face 90×80 + bubble 500×50
};
const DEFAULT_OVERLAY_STYLE = 'amoled';
const OVERLAY_POSITIONS = ['bottom', 'top', 'center'];
const OVERLAY_ACCENTS = ['mint', 'mono', 'aurora', 'blue', 'violet', 'red', 'amber'];
// Retired style ids → [successor, position]. Pre-2.6 the AMOLED placement was
// baked into the id; 2.7 replaced the classic visualizers (and dropped the Orb).
const LEGACY_OVERLAY_STYLES = {
  amoled_top:    ['amoled', 'top'],
  amoled_bottom: ['amoled', 'bottom'],
  amoled_center: ['amoled', 'center'],
  amoled_card:   ['amoled', null],
  wave:          ['amoled', null],
  wave_classic:  ['amoled', null],
  ribbon:        ['amoled', null],
  particles:     ['amoled', null],
  spectrum:      ['amoled', null],
  aurora:        ['halo', null],
  pulse:         ['halo', null],
  orb:           ['halo', null],
};

// Map legacy style ids onto style + position and fill defaults.
// Returns true when something changed (caller persists).
function normalizeOverlayConfig() {
  let changed = false;
  const legacy = LEGACY_OVERLAY_STYLES[config.overlay_style];
  if (legacy) {
    config.overlay_style = legacy[0];
    if (legacy[1] && !OVERLAY_POSITIONS.includes(config.overlay_position)) config.overlay_position = legacy[1];
    changed = true;
  }
  if (!OVERLAY_STYLES[config.overlay_style]) {
    config.overlay_style = DEFAULT_OVERLAY_STYLE;
    changed = true;
  }
  if (!OVERLAY_POSITIONS.includes(config.overlay_position)) {
    config.overlay_position = 'bottom';
    changed = true;
  }
  if (!OVERLAY_ACCENTS.includes(config.overlay_accent)) {
    config.overlay_accent = 'mint';
    changed = true;
  }
  return changed;
}

function getStyleConfig(name) {
  return OVERLAY_STYLES[name] || OVERLAY_STYLES[DEFAULT_OVERLAY_STYLE];
}

// Apply the right size + anchor for the current overlay style.
function applyOverlayBounds() {
  if (!overlayWin || overlayWin.isDestroyed()) return;
  const s = getStyleConfig(config.overlay_style);

  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);
  const { x, y, width, height } = display.workArea;

  overlayWin.setBounds({ width: s.w, height: s.h, x: 0, y: 0 });

  const posX = x + Math.round((width - s.w) / 2);
  let posY;
  switch (config.overlay_position) {
    case 'top':    posY = y + s.top; break;
    case 'center': posY = y + Math.round((height - s.h) / 2); break;
    case 'bottom':
    default:       posY = y + height - s.h - s.bottom; break;
  }
  overlayWin.setPosition(posX, posY);
}

function createOverlay() {
  const initial = getStyleConfig(config.overlay_style);
  overlayWin = new BrowserWindow({
    width: initial.w,
    height: initial.h,
    show: false,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    focusable: false,
    hasShadow: false,
    type: 'toolbar',  // No taskbar entry
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  overlayReady = false;
  overlayWin.setIgnoreMouseEvents(true);
  overlayWin.loadFile(path.join(__dirname, 'overlay.html'));
  overlayWin.webContents.once('did-finish-load', () => { overlayReady = true; });

  overlayWin.on('closed', () => {
    overlayWin = null;
    overlayReady = false;
  });
}

// Push the current style/theme + recording-started to the overlay. If the
// renderer isn't loaded yet (freshly recreated window), wait for did-finish-load
// so the IPC messages are never dropped.
function pushOverlayState() {
  if (!overlayWin || overlayWin.isDestroyed()) return;
  const styleName = OVERLAY_STYLES[config.overlay_style] ? config.overlay_style : DEFAULT_OVERLAY_STYLE;
  const send = () => {
    if (!overlayWin || overlayWin.isDestroyed()) return;
    overlayWin.webContents.send('preview-enabled', config.live_preview_enabled !== false);
    overlayWin.webContents.send('app-lang', config.app_language || 'de');
    overlayWin.webContents.send('set-style', styleName, {
      accent: config.overlay_accent || 'mint',
      position: config.overlay_position || 'bottom',
      sounds: config.overlay_sounds !== false,
    });
    overlayWin.webContents.send('set-theme', currentTheme());
    overlayWin.webContents.send('recording-started', { command: commandActive });
  };
  if (overlayReady) send();
  else overlayWin.webContents.once('did-finish-load', send);
}

let overlayHideTimer = null;

function showOverlay() {
  if (overlayIdleTimer) { clearTimeout(overlayIdleTimer); overlayIdleTimer = null; }
  if (overlayHideTimer) { clearTimeout(overlayHideTimer); overlayHideTimer = null; }
  if (!overlayWin || overlayWin.isDestroyed()) createOverlay();
  applyOverlayBounds();
  overlayWin.showInactive();
  pushOverlayState();
}

function hideOverlay() {
  if (overlayWin && !overlayWin.isDestroyed()) {
    // Short fade-out in the renderer before the window disappears – a hard
    // hide reads as a glitch next to the animated entry.
    if (overlayHideTimer) clearTimeout(overlayHideTimer);
    if (overlayWin.isVisible()) {
      try { overlayWin.webContents.send('overlay-leaving'); } catch (_) {}
      overlayHideTimer = setTimeout(() => {
        overlayHideTimer = null;
        if (overlayWin && !overlayWin.isDestroyed() && !isRecording) overlayWin.hide();
      }, 230);
    }
    // Destroy the transparent always-on-top surface after a long idle period
    // so it isn't kept alive for the whole session. Recreated lazily on next
    // record. (Hidden cost is already ~0 thanks to the document.hidden gate;
    // this just frees the compositor surface during extended non-use.)
    if (overlayIdleTimer) clearTimeout(overlayIdleTimer);
    overlayIdleTimer = setTimeout(() => {
      overlayIdleTimer = null;
      if (overlayWin && !overlayWin.isDestroyed() && !overlayWin.isVisible()) {
        overlayWin.destroy();
        overlayWin = null;
        overlayReady = false;
      }
    }, OVERLAY_IDLE_MS);
  }
}

// ── Hotkey Handling ──
function configHotkeyToElectron(hotkey) {
  if (!hotkey) return 'CmdOrCtrl+Shift+Space';
  return hotkey
    .replace(/\bctrl\b/gi, 'CmdOrCtrl')
    .replace(/\balt\b/gi, 'Alt')
    .replace(/\bshift\b/gi, 'Shift')
    .replace(/\bspace\b/gi, 'Space')
    .replace(/\benter\b/gi, 'Return')
    .replace(/\btab\b/gi, 'Tab')
    .replace(/\bdelete\b/gi, 'Delete')
    .replace(/\bescape\b/gi, 'Escape')
    .replace(/\bbackspace\b/gi, 'Backspace')
    .replace(/\bless\b/gi, '<')
    .split('+')
    .map(p => p.length === 1 ? p.toUpperCase() : p)
    .join('+');
}

let holdTimer = null;  // Timer for hold-to-speak release detection

// One handler for both hotkeys: dictation and command mode (`command`).
function onHotkey(command) {
  const mode = config.mode || 'press_to_speak';
  const keys = command ? config.command_hotkey : config.hotkey;
  if (mode === 'hold_to_speak') {
    // Hold-to-speak: globalShortcut fires repeatedly while held.
    // Start recording on first fire, reset release-detection timer each fire.
    if (!isRecording && !isTranscribing) {
      startRecording({ command });
      // Precise release: the backend polls the hotkey's virtual keys
      // (GetAsyncKeyState, ~15 ms) and sends 'hotkey_released'.
      // globalShortcut has no key-up event – the old 350 ms-after-last-
      // repeat timer added 350 ms to every dictation and, when the key
      // was held shorter than the OS repeat delay (~500 ms), fired before
      // the first repeat and cut the recording off mid-word.
      if (isRecording) sendToPython('watch_hotkey_release', { hotkey: keys });
    }
    // The other hotkey's repeats must not keep this recording alive.
    if (isRecording && commandActive !== command) return;
    // Timer is now only a fallback (hotkey not mappable by the backend):
    // long enough that it can't fire before OS key-repeat kicks in.
    if (holdTimer) clearTimeout(holdTimer);
    holdTimer = setTimeout(() => {
      holdTimer = null;
      if (isRecording) stopRecording();
    }, holdReleaseViaBackend ? 1200 : 350);
  } else {
    // Press-to-speak: toggle
    if (!isRecording) {
      startRecording({ command });
    } else {
      stopRecording();
    }
  }
}

function registerHotkeys() {
  globalShortcut.unregisterAll();
  if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }

  const hotkey = configHotkeyToElectron(config.hotkey);
  const mode = config.mode || 'press_to_speak';
  console.log(`Registering hotkey: ${hotkey} (mode: ${mode})`);

  try {
    const success = globalShortcut.register(hotkey, () => onHotkey(false));

    if (!success) {
      console.error(`✗ Failed to register hotkey: ${hotkey}`);
      globalShortcut.register('CmdOrCtrl+Shift+Space', () => {
        if (!isRecording) startRecording();
        else stopRecording();
      });
      console.log('Using fallback: CmdOrCtrl+Shift+Space');
    } else {
      console.log(`✓ Hotkey registered: ${hotkey} (${mode})`);
    }
  } catch (e) {
    console.error('Hotkey registration error:', e);
  }

  // Command mode: select text, hold the second hotkey, speak an instruction.
  if (config.command_enabled !== false && config.command_hotkey) {
    const cmdKey = configHotkeyToElectron(config.command_hotkey);
    try {
      if (cmdKey.toLowerCase() === hotkey.toLowerCase()) {
        console.error(`✗ Command hotkey equals the dictation hotkey (${cmdKey}) – skipped`);
      } else if (globalShortcut.register(cmdKey, () => onHotkey(true))) {
        console.log(`✓ Command hotkey registered: ${cmdKey}`);
      } else {
        console.error(`✗ Failed to register command hotkey: ${cmdKey}`);
      }
    } catch (e) {
      console.error('Command hotkey registration error:', e);
    }
  }
}

function startRecording({ command = false } = {}) {
  if (isTranscribing) {
    // Previous dictation is still being transcribed. The press is ignored,
    // but not silently: a low blip + nudge on the (still visible) overlay.
    // Throttled – in hold mode the OS key-repeat calls this ~30×/s.
    const now = Date.now();
    if (now - lastBusyTs > 700 && overlayWin && !overlayWin.isDestroyed()) {
      lastBusyTs = now;
      overlayWin.webContents.send('busy');
    }
    return;
  }
  if (isRecording) return;     // Already recording – ignore re-trigger
  if (!modelLoaded) {
    showToast(trayT().modelNotReady, 'warning');
    return;
  }
  isRecording = true;
  commandActive = !!command;
  closeSuggestion();  // a new dictation supersedes it (and the overlay sits there)
  sendToOnboarding('recording-started');
  isPaused = false;
  showOverlay();
  sendToPython('start_recording');
  updateTray('recording');
}

function stopRecording() {
  if (!isRecording) return;    // Nothing to stop
  isRecording = false;
  isPaused = false;
  isTranscribing = true;  // Prevent re-trigger during transcription
  // The overlay stays up in a "processing" state until the final pass is
  // done (finishTranscription hides it) – the flow reads record → process →
  // inserted instead of the indicator vanishing on key-release.
  if (overlayWin && !overlayWin.isDestroyed()) {
    overlayWin.webContents.send('recording-stopped');
  }
  sendToOnboarding('recording-stopped');
  // Unregister hotkeys during paste to prevent Ctrl+V from triggering
  globalShortcut.unregisterAll();
  recordStopTs = Date.now();
  sendToPython(commandActive ? 'stop_command' : 'stop_recording');
  updateTray('processing');

  // Safety net: if the backend NEVER replies (process crashed, IPC stuck),
  // make sure we unlock after 30 s instead of leaving the user dead. A
  // command also waits on the LLM (up to 25 s) – give it more room.
  setTimeout(() => {
    if (isTranscribing) {
      console.error('Transcription timeout – forcing state reset');
      finishTranscription();
      showToast(trayT().transcribeStuck, 'error');
    }
  }, commandActive ? 45_000 : 30_000);
}

// Single place to reset post-recording state. Called from `transcription_done`,
// from `error`, and from the timeout watchdog above.
function finishTranscription(ok = false, text = '') {
  isRecording = false;
  isTranscribing = false;
  isPaused = false;
  const showText = ok && !!text && config.live_preview_enabled !== false;
  if (overlayWin && !overlayWin.isDestroyed()) {
    overlayWin.webContents.send('transcription-done', { ok, text: showText ? String(text) : '' });
  }
  // Success: the overlay shows the inserted sentence for a moment (mint
  // "done" state) before fading – long enough to read what landed, short
  // enough not to be in the way. Without live text: just a brief flash.
  // Guard: the user may already be recording again by then.
  const hold = showText ? 1400 : (ok ? 420 : 0);
  if (hold) setTimeout(() => { if (!isRecording) hideOverlay(); }, hold);
  else hideOverlay();
  registerHotkeys();
  updateTray('ready');
}

function togglePause() {
  if (!isRecording) return;
  if (isPaused) {
    sendToPython('resume_recording');
  } else {
    sendToPython('pause_recording');
  }
}

// ── Tray Icon ──
// We load the pre-rendered tray PNG (32px) and overlay a tiny status dot in
// software for the recording/processing/paused states. Loading the PNG once is
// way cheaper and prettier than the previous per-pixel RGBA loop.
let _trayBaseImage = null;
function getTrayBaseImage() {
  if (_trayBaseImage) return _trayBaseImage;
  const candidates = [
    path.join(__dirname, 'assets', 'tray-32.png'),
    path.join(__dirname, 'assets', 'icon.png'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) {
      _trayBaseImage = nativeImage.createFromPath(p).resize({ width: 32, height: 32 });
      return _trayBaseImage;
    }
  }
  // Fallback: empty 32x32 transparent icon (rather than crashing)
  _trayBaseImage = nativeImage.createEmpty();
  return _trayBaseImage;
}

function createTrayImage(color = '#00d4aa', recording = false) {
  const base = getTrayBaseImage();
  if (!recording) return base;

  // For the recording state, draw a red dot in the upper-right corner of the
  // base PNG. `toBitmap()` returns BGRA on Windows; `createFromBuffer` expects
  // the same layout when given a width+height descriptor – so we write BGRA
  // and don't swap channels. (Bugfix: previous version produced a blue dot.)
  const s = 32;
  const buf = Buffer.from(base.toBitmap());
  const cx = s - 6, cy = 6, r = 4;
  for (let y = Math.max(0, cy - r); y < Math.min(s, cy + r + 1); y++) {
    for (let x = Math.max(0, cx - r); x < Math.min(s, cx + r + 1); x++) {
      const dx = x - cx, dy = y - cy;
      if (dx * dx + dy * dy <= r * r) {
        const i = (y * s + x) * 4;
        // BGRA: red = (B=68, G=68, R=239) – #ef4444
        buf[i]     = 68;   // B
        buf[i + 1] = 68;   // G
        buf[i + 2] = 239;  // R
        buf[i + 3] = 255;  // A
      }
    }
  }
  return nativeImage.createFromBuffer(buf, { width: s, height: s });
}

function createTray() {
  const icon = createTrayImage('#00d4aa');
  tray = new Tray(icon);
  tray.setToolTip('NoType – Bereit');
  tray.setContextMenu(buildTrayMenu('ready'));
}

// Boot-time autostart sometimes fires before Explorer is ready to accept tray
// icons. Retry a few times with backoff so we don't end up running headlessly.
function createTrayWithRetry(attempt = 0) {
  try {
    createTray();
    console.log(`✓ Tray created (attempt ${attempt + 1})`);
  } catch (e) {
    console.error(`Tray creation failed (attempt ${attempt + 1}):`, e.message);
    if (attempt < 8) {
      setTimeout(() => createTrayWithRetry(attempt + 1), 1500);
    }
  }
}

function updateTray(status) {
  if (!tray) return;
  const colors = { ready: '#00d4aa', recording: '#ff4444', processing: '#ffaa00', loading: '#888888', paused: '#ffaa00' };
  const T = trayT();
  const labels = {
    ready: T.ready,
    recording: T.recording,
    processing: T.processing,
    loading: T.loading,
    paused: T.paused,
  };

  // Override status if model not loaded
  if (!modelLoaded && status === 'ready') status = 'loading';

  try {
    tray.setImage(createTrayImage(colors[status] || '#00d4aa', status === 'recording'));
    const hotkeyLabel = config.hotkey || 'Ctrl+Shift+Space';
    tray.setToolTip(`NoType – ${labels[status] || T.ready} (${hotkeyLabel})`);
    tray.setContextMenu(buildTrayMenu(status));
  } catch (e) {
    console.error('Tray update error:', e);
  }
}

// Minimal tray/toast i18n – the settings window has its own dictionary.
function trayT() {
  const de = {
    ready: '✓ Bereit', recording: '● Aufnahme...', processing: '⟳ Transkribiere...',
    loading: '⏳ Modell lädt...', paused: '⏸ Pausiert',
    today: 'Heute', words: 'Wörter', transcriptions: 'Transkriptionen',
    last7: 'Letzte 7 Tage', last30: 'Letzte 30 Tage',
    history: 'Letzte Transkriptionen', settings: '⚙  Einstellungen', setup: '✦  Einrichtung', quit: '✕  Beenden',
    tooShort: 'Aufnahme zu kurz – verworfen',
    backendCrashed: 'Backend abgestürzt – starte neu',
    backendDead: 'Backend startet nicht – bitte NoType neu starten',
    modelLoading: 'Lade Modell {model} …', modelReady: '{model} ist bereit',
    modelNotReady: 'Modell lädt noch – bitte kurz warten',
    transcribeStuck: 'Transkription hängt – Status zurückgesetzt',
    bootReady: 'NoType bereit · {model} · {device}',
    commandNoLlm: 'Befehlsmodus braucht die lokale KI – Einstellungen → Text-Bereinigung',
    commandFailed: 'Befehl fehlgeschlagen – die KI hat nicht rechtzeitig geantwortet',
    locale: 'de-DE',
  };
  const en = {
    ready: '✓ Ready', recording: '● Recording...', processing: '⟳ Transcribing...',
    loading: '⏳ Loading model...', paused: '⏸ Paused',
    today: 'Today', words: 'words', transcriptions: 'transcriptions',
    last7: 'Last 7 days', last30: 'Last 30 days',
    history: 'Recent transcriptions', settings: '⚙  Settings', setup: '✦  Setup', quit: '✕  Quit',
    tooShort: 'Recording too short – discarded',
    backendCrashed: 'Backend crashed – restarting',
    backendDead: 'Backend won\'t start – please restart NoType',
    modelLoading: 'Loading model {model} …', modelReady: '{model} is ready',
    modelNotReady: 'Model still loading – one moment',
    transcribeStuck: 'Transcription stalled – state reset',
    bootReady: 'NoType ready · {model} · {device}',
    commandNoLlm: 'Command mode needs the local AI – Settings → Text Cleanup',
    commandFailed: 'Command failed – the AI did not answer in time',
    locale: 'en-US',
  };
  return config.app_language === 'en' ? en : de;
}

const MODEL_LABELS = {
  'tiny': 'Whisper Tiny', 'base': 'Whisper Base', 'small': 'Whisper Small',
  'medium': 'Whisper Medium', 'large-v2': 'Whisper Large v2', 'large-v3': 'Whisper Large v3',
  'large-v3-turbo': 'Whisper Large v3 Turbo', 'distil-large-v3': 'Distil Large v3',
  'distil-large-v3.5': 'Distil Large v3.5', 'german-turbo': 'Large v3 Turbo German',
};
function modelLabel(id) { return MODEL_LABELS[id] || id; }

function buildTrayMenu(status = 'ready') {
  const T = trayT();
  const labels = {
    ready: T.ready,
    recording: T.recording,
    processing: T.processing,
    loading: T.loading,
    paused: T.paused,
  };
  const hotkeyLabel = config.hotkey || 'Ctrl+Shift+Space';

  // Reset stats if new day
  if (todayStats.date !== todayKey()) {
    todayStats = { date: todayKey(), count: 0, words: 0 };
  }

  // Aggregate stats across 7-day and 30-day windows for the submenu.
  const now = Date.now();
  const sumRange = (days) => {
    const cutoff = now - days * 24 * 3600 * 1000;
    let count = 0, words = 0;
    for (const [date, s] of Object.entries(statsByDate)) {
      const t = Date.parse(date);
      if (isNaN(t) || t < cutoff) continue;
      count += s.count || 0;
      words += s.words || 0;
    }
    return { count, words };
  };
  const w7 = sumRange(7);
  const w30 = sumRange(30);

  const items = [
    { label: `NoType – ${labels[status] || T.ready}`, enabled: false },
    { label: `Hotkey: ${hotkeyLabel}`, enabled: false },
    {
      label: `${T.today}: ${todayStats.count} · ~${todayStats.words} ${T.words}`,
      submenu: [
        { label: `${T.today}: ${todayStats.count} ${T.transcriptions}, ~${todayStats.words} ${T.words}`, enabled: false },
        { label: `${T.last7}: ${w7.count} ${T.transcriptions}, ~${w7.words} ${T.words}`, enabled: false },
        { label: `${T.last30}: ${w30.count} ${T.transcriptions}, ~${w30.words} ${T.words}`, enabled: false },
      ],
    },
    { type: 'separator' },
  ];

  // Clipboard history
  if (clipboardHistory.length > 0) {
    items.push({ label: T.history, enabled: false });
    clipboardHistory.forEach((entry, i) => {
      const preview = entry.text.length > 45 ? entry.text.slice(0, 45) + '…' : entry.text;
      const time = new Date(entry.time).toLocaleTimeString(T.locale, { hour: '2-digit', minute: '2-digit' });
      items.push({
        label: `  ${time}  ${preview}`,
        click: () => {
          require('electron').clipboard.writeText(entry.text);
        },
      });
    });
    items.push({ type: 'separator' });
  }

  items.push(
    { label: T.settings, click: () => openSettings() },
    { label: T.setup, click: () => openOnboarding() },
    { type: 'separator' },
    { label: T.quit, click: () => quitApp() },
  );

  return Menu.buildFromTemplate(items);
}

// ── Settings Window ──
function openSettings() {
  if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.focus();
    return;
  }

  settingsWin = new BrowserWindow({
    width: 920,
    height: 700,
    minWidth: 780,
    minHeight: 560,
    show: false,
    resizable: true,
    frame: false,
    // Transparent backgroundColor lets the Mica material show through on Win11.
    backgroundColor: '#00000000',
    backgroundMaterial: 'mica',  // Win11 only, ignored elsewhere
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  settingsWin.loadFile(path.join(__dirname, 'settings.html'));
  settingsWin.once('ready-to-show', () => {
    settingsWin.webContents.send('set-theme', currentTheme());
    settingsWin.show();
    applyMica(settingsWin);
    pushConfigToSettings();
    // Sync model status
    if (modelLoaded && modelInfo) {
      settingsWin.webContents.send('model-ready', modelInfo);
    }
  });

  settingsWin.on('closed', () => {
    settingsWin = null;
    // A hotkey capture may have been active – make sure the shortcut is back.
    if (!isRecording && !isTranscribing) registerHotkeys();
  });
}

// ── First-run wizard ──
// Mic → hotkey → model → live test → done. Also reachable later from the
// tray and the settings page. Saves each step immediately (partial config
// merges), so the test step dictates with the real, freshly chosen setup.
function openOnboarding() {
  if (onboardingWin && !onboardingWin.isDestroyed()) {
    onboardingWin.focus();
    return;
  }
  onboardingWin = new BrowserWindow({
    width: 800,
    height: 640,
    minWidth: 720,
    minHeight: 580,
    show: false,
    resizable: true,
    frame: false,
    backgroundColor: '#00000000',
    backgroundMaterial: 'mica',
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  onboardingWin.loadFile(path.join(__dirname, 'onboarding.html'));
  onboardingWin.once('ready-to-show', () => {
    if (!onboardingWin || onboardingWin.isDestroyed()) return;
    onboardingWin.webContents.send('set-theme', currentTheme());
    onboardingWin.show();
    applyMica(onboardingWin);
    onboardingWin.webContents.send('config', config);
    if (modelLoaded && modelInfo) onboardingWin.webContents.send('model-ready', modelInfo);
  });
  onboardingWin.on('closed', () => {
    onboardingWin = null;
    markOnboardingDone();   // Alt+F4 counts as "skip" – don't nag next launch
    if (!isRecording && !isTranscribing) registerHotkeys();
  });
}

function markOnboardingDone() {
  if (config.first_run_complete) return;
  config.first_run_complete = true;
  writeConfigToDisk();
  if (pythonProcess && pythonProcess.stdin.writable) sendToPython('save_config', { first_run_complete: true });
}

function sendToOnboarding(channel, data) {
  if (onboardingWin && !onboardingWin.isDestroyed()) {
    try { onboardingWin.webContents.send(channel, data); } catch (_) { /* window closing */ }
  }
}

// ── IPC from renderers ──
// Settings need a config even when the backend is down – answer from our own
// copy then, instead of letting the form open blank.
function pushConfigToSettings() {
  if (pythonProcess && pythonProcess.stdin.writable) sendToPython('get_config');
  else if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('config', config);
}

ipcMain.on('save-config', (_, newConfig) => {
  // 1) Persist FIRST, here in the main process. This used to only forward to
  //    the backend – and sendToPython() silently drops the message while the
  //    backend is dead or in restart back-off. So a user who picked a smaller
  //    model *because* the big one kept crashing lost exactly that change.
  const prevModel = config.model_size;
  const prevHotkey = config.hotkey, prevMode = config.mode;
  const prevCmd = `${config.command_enabled}|${config.command_hotkey}`;
  config = { ...config, ...newConfig };
  writeConfigToDisk();
  if (config.hotkey !== prevHotkey || config.mode !== prevMode ||
      `${config.command_enabled}|${config.command_hotkey}` !== prevCmd) registerHotkeys();

  const modelChanged = !!newConfig.model_size && newConfig.model_size !== prevModel;
  if (modelChanged) {
    pendingModelSwitch = newConfig.model_size;
    modelLoaded = false;
    updateTray('loading');
    showToast(trayT().modelLoading.replace('{model}', modelLabel(newConfig.model_size)), 'warning');
  }

  // 2) Tell a live backend; revive a dead one. A (re)started backend reads
  //    settings.json, so the change takes effect either way – and a model
  //    downgrade is precisely the fix for a backend that crashed on a model
  //    too big for the machine, so the circuit breaker gets a fresh start.
  if (pythonProcess && pythonProcess.stdin.writable) {
    sendToPython('save_config', newConfig);
  } else if (!quitRequested) {
    consecutiveFastCrashes = 0;
    crashToastShown = false;
    backendGaveUp = false;
    startPython();
  }
});
ipcMain.on('get-config', () => pushConfigToSettings());
ipcMain.on('get-history', () => pushHistoryToSettings());
ipcMain.on('list-devices', () => sendToPython('list_devices'));
ipcMain.on('list-models', () => sendToPython('list_models'));
ipcMain.on('hotkey-capture', (_, active) => {
  // A page is recording a new key combo – the global shortcut must not fire
  // (and start a dictation) while the user presses keys to define it.
  if (active) globalShortcut.unregisterAll();
  else if (!isRecording && !isTranscribing) registerHotkeys();
});
ipcMain.on('open-onboarding', () => openOnboarding());
ipcMain.on('open-settings', () => openSettings());
ipcMain.on('close-onboarding', () => {
  // Finished or dismissed – either way, don't show the wizard again.
  markOnboardingDone();
  if (onboardingWin && !onboardingWin.isDestroyed()) onboardingWin.close();
});
ipcMain.on('ollama-status', () => sendToPython('ollama_status'));
ipcMain.on('ollama-setup', () => sendToPython('ollama_setup'));
ipcMain.on('ollama-update', () => sendToPython('ollama_update'));
ipcMain.on('close-settings', () => {
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.close();
});
ipcMain.on('close-toast', () => {
  if (toastWin && !toastWin.isDestroyed()) toastWin.close();
});

// ── Correction suggestions (dictionary stage 2) ──
// The backend spotted a fix the user typed right after a dictation. The pill
// is clickable but never takes focus – the user keeps typing where they were.
const DICT_MAX = 500;
const phraseKey = (s) => (String(s).toLowerCase().match(/[\p{L}\p{N}_]+/gu) || []).join(' ');

function closeSuggestion() {
  if (suggestTimer) { clearTimeout(suggestTimer); suggestTimer = null; }
  if (suggestWin && !suggestWin.isDestroyed()) suggestWin.close();
  suggestWin = null;
}

function armSuggestTimer(ms) {
  if (suggestTimer) clearTimeout(suggestTimer);
  suggestTimer = setTimeout(closeSuggestion, ms);
}

function showSuggestion(items) {
  items = (items || []).filter(it => it && it.from && it.to).slice(0, 3);
  if (!items.length) return;
  closeSuggestion();
  suggestItems = items;
  const win = new BrowserWindow({
    width: 620,
    height: 72,
    show: false,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    focusable: false,
    hasShadow: false,
    type: 'toolbar',
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  suggestWin = win;
  // Only the pill takes clicks; the transparent rest of the window passes
  // them through (toggled from the page's hover events).
  win.setIgnoreMouseEvents(true, { forward: true });
  win.loadFile(path.join(__dirname, 'suggest.html'));
  win.once('ready-to-show', () => {
    if (win.isDestroyed()) return;
    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    const { x, y, width, height } = display.workArea;
    const b = win.getBounds();
    win.setPosition(x + Math.round((width - b.width) / 2), y + height - b.height - 44);
    win.webContents.send('set-theme', currentTheme());
    win.webContents.send('suggest-data', { items, lang: config.app_language === 'en' ? 'en' : 'de' });
    win.showInactive();
  });
  armSuggestTimer(10_000);
  win.on('closed', () => { if (suggestWin === win) closeSuggestion(); });
}

// Same rules as adding in Settings → Wörterbuch: a new spelling for a known
// mistake replaces the old one, the exact reverse pair goes.
function learnDictionary(items) {
  let entries = Array.isArray(config.dictionary) ? config.dictionary.slice() : [];
  for (const { from, to } of items) {
    const fk = phraseKey(from), tk = phraseKey(to);
    if (!fk || !tk || from === to) continue;
    entries = entries.filter(e =>
      phraseKey(e.from) !== fk && !(phraseKey(e.from) === tk && phraseKey(e.to) === fk));
    if (entries.length >= DICT_MAX) break;
    entries.push({ from, to });
  }
  config.dictionary = entries;
  writeConfigToDisk();
  sendToPython('save_config', { dictionary: entries });
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('dictionary', entries);
}

ipcMain.on('suggest-hover', (_, hovering) => {
  if (!suggestWin || suggestWin.isDestroyed()) return;
  suggestWin.setIgnoreMouseEvents(!hovering, { forward: true });
  // Reading or aiming at the pill keeps it; leaving gives a short grace.
  if (hovering) { if (suggestTimer) clearTimeout(suggestTimer); suggestTimer = null; }
  else armSuggestTimer(4000);
});

ipcMain.on('suggest-answer', (_, learn) => {
  if (!learn) { closeSuggestion(); return; }
  learnDictionary(suggestItems);
  suggestItems = [];
  armSuggestTimer(1800);  // the pill shows "learned" first
});

// ── Stats Tracking ──
function trackStats(text) {
  // Reset today's bucket if the day has rolled over
  const tk = todayKey();
  if (todayStats.date !== tk) {
    todayStats = { date: tk, count: 0, words: 0 };
  }
  todayStats.count++;
  todayStats.words += text.trim().split(/\s+/).length;
  scheduleSave();  // also persists statsByDate
}

// ── Clipboard History ──
function addToHistory(text) {
  clipboardHistory.unshift({ text, time: Date.now() });
  if (clipboardHistory.length > MAX_HISTORY) clipboardHistory.pop();
  scheduleSave();
  updateTray('ready');  // Refresh menu with new history
  pushHistoryToSettings();  // dictionary pane: learn from recent dictations
}

function pushHistoryToSettings() {
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('history', clipboardHistory);
}

// ── Toast Notification ──
function showToast(text, type = 'success', stats = null) {
  if (toastWin && !toastWin.isDestroyed()) toastWin.close();

  // Local reference for all handlers below. The global `toastWin` can be
  // nulled/replaced while this window is still loading (a newer toast closes
  // this one; the async 'closed' handler then nulls the global) – handlers
  // touching the global raced that and crashed with "getBounds of null".
  const win = new BrowserWindow({
    width: 520,
    height: 56,
    show: false,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    focusable: false,
    hasShadow: false,
    type: 'toolbar',
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  toastWin = win;
  win.setIgnoreMouseEvents(true);
  win.loadFile(path.join(__dirname, 'toast.html'));

  win.once('ready-to-show', () => {
    if (win.isDestroyed()) return;
    const cursor = screen.getCursorScreenPoint();
    const display = screen.getDisplayNearestPoint(cursor);
    const { x, y, width, height } = display.workArea;
    const b = win.getBounds();
    win.setPosition(
      x + Math.round((width - b.width) / 2),
      y + height - b.height - 50
    );
    win.webContents.send('set-theme', currentTheme());
    win.showInactive();
    win.webContents.send('toast-data', { text, type, stats, lang: config.app_language === 'en' ? 'en' : 'de' });
  });

  // Auto-close after 3 seconds
  setTimeout(() => {
    if (!win.isDestroyed()) win.close();
  }, 3000);

  win.on('closed', () => { if (toastWin === win) toastWin = null; });
}

// ── App Lifecycle ──
function quitApp() {
  quitRequested = true;  // suppress backend auto-restart
  if (_saveTimer) { clearTimeout(_saveTimer); _saveTimer = null; }
  saveHistory();  // final synchronous flush of any pending debounced save
  sendToPython('quit');
  globalShortcut.unregisterAll();
  setTimeout(() => {
    if (pythonProcess) try { pythonProcess.kill(); } catch (e) {}
    if (overlayWin) try { overlayWin.close(); } catch (e) {}
    if (settingsWin) try { settingsWin.close(); } catch (e) {}
    if (tray) try { tray.destroy(); } catch (e) {}
    app.quit();
  }, 500);
}

app.whenReady().then(() => {
  initPaths();
  loadConfig();
  loadHistory();
  // Overlay is created lazily on the first recording (showOverlay) so no
  // transparent always-on-top compositor surface exists until actually needed.
  createTrayWithRetry();
  startPython();
  startBackendWatchdog();
  // Register hotkeys immediately – globalShortcut doesn't depend on the Python backend.
  // The backend may later push an updated config that re-registers via `config_saved`.
  registerHotkeys();

  // First launch: guided setup (mic → hotkey → model → test). Tray and
  // backend are already starting, so the wizard's test step works for real.
  if (!config.first_run_complete) setTimeout(openOnboarding, 600);

  // React to system theme changes (Win11 light/dark switch)
  nativeTheme.on('updated', broadcastTheme);
  // Push the initial theme once windows are ready
  setTimeout(broadcastTheme, 200);

  console.log('✓ NoType Electron app ready');
});

// When a second instance tries to launch, just open the settings window of this one.
app.on('second-instance', () => {
  if (settingsWin && !settingsWin.isDestroyed()) {
    if (settingsWin.isMinimized()) settingsWin.restore();
    settingsWin.focus();
  } else {
    openSettings();
  }
});

app.on('window-all-closed', (e) => e.preventDefault());
app.on('will-quit', () => globalShortcut.unregisterAll());
app.on('before-quit', () => sendToPython('quit'));
