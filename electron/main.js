const { app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, nativeImage, screen, nativeTheme, shell, session } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const fs = require('fs');
const { loadSettingsFile, readSettingsText } = require('./lib/settings-file');
const { parseBackendLine } = require('./lib/backend-line');
const { isSameDocument, isAppFile, allowPermission } = require('./lib/guards');
const { phraseKey } = require('./lib/phrase-key');
const { modelLabel } = require('./lib/model-labels');
const { normalizeFailure, failureText, failureAction, backendFailure } = require('./lib/model-status');
const { afterExit } = require('./lib/backend-breaker');
const { loadHistoryFile, saveHistoryFile, debouncedSave } = require('./lib/history-file');
const { firstUpdateCheck } = require('./lib/first-update-check');
const { senderPage, allowSender, filterConfig } = require('./lib/ipc-guard');

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
  const _cfg = fs.existsSync(_cfgPath) ? JSON.parse(readSettingsText(_cfgPath)) : {};
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
// File shape and the save_history switch: see lib/history-file.js.
function todayKey() { return new Date().toDateString(); }

function historyEnabled() { return config.save_history !== false; }

function loadHistory() {
  const r = loadHistoryFile(HISTORY_FILE, { enabled: historyEnabled(), max: MAX_HISTORY });
  if (r.error) console.error('History load failed (starting fresh):', r.error);
  clipboardHistory = r.history;
  statsByDate = r.stats;
  const tk = todayKey();
  todayStats = statsByDate[tk] || { date: tk, count: 0, words: 0 };
  todayStats.date = tk;  // ensure shape
  // Switched off: drop dictations stored before (stats are loaded by now).
  if (r.rewrite) saveHistory();
}

function saveHistory() {
  if (!HISTORY_FILE) return;  // initPaths() not yet called
  // Roll today's stats into the persisted map before writing.
  statsByDate[todayStats.date] = { count: todayStats.count, words: todayStats.words };
  try {
    saveHistoryFile(HISTORY_FILE, { history: clipboardHistory, stats: statsByDate, enabled: historyEnabled() });
  } catch (e) {
    console.error('History save failed:', e.message);
  }
}

// Each transcription calls both addToHistory and trackStats: one write ~1s
// later, off the post-transcription hot path. quitApp() flushes synchronously.
const historySaver = debouncedSave(saveHistory, 1000);

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
let consecutiveFastCrashes = 0;    // backends that died <10s after start, in a row (circuit breaker)
let crashToastShown = false;       // rate-limit the crash toast during a storm
let backendUptimeTimer = null;     // resets the breaker after sustained uptime
let backendGaveUp = false;         // true once we stop auto-restarting
let quitRequested = false;         // set by quitApp() to suppress restart
let isRecording = false;
let isPaused = false;
let isTranscribing = false;
let modelLoaded = false;
let modelInfo = null;
let modelFailure = null;         // why no model could be loaded (lib/model-status.js), until one loads
let pendingModelSwitch = null;   // model id the user just switched to → "ready" toast
let bootReadyToastShown = false; // one "NoType ready" toast per launch
let recordStopTs = 0;            // when the user released – for the insert-latency stat
let holdReleaseViaBackend = true;// backend reports real key-up (optimistic; see 'hotkey_watch')
let onboardingWin = null;        // first-run wizard (also reachable from settings/tray)
let lastBusyTs = 0;              // throttle for the "still processing" nudge
let commandActive = false;       // current recording is a command-mode instruction
let agentActive = false;         // current recording goes to the agent URL instead of being typed
let agentHintUntil = 0;          // agent hotkey without a URL: one hint per press, not per key repeat
let hotkeyQuietUntil = 0;        // hold mode: ignore the still-held hotkey after an Esc cancel
let modelHintUntil = 0;          // no model yet: one "loading"/"failed" toast per hotkey press
let attachments = [];            // AI context chips of the current dictation (replayed into a fresh overlay)
let config = {};
let clipboardHistory = [];  // Last 10 transcriptions
const MAX_HISTORY = 10;

// Saved recordings (the backend's rescue folder): what is waiting to be
// transcribed, which one is running, which ones the user already heard about.
let rescueItems = [];
let rescueDir = '';
let rescueRunning = null;
let rescueOfferPending = false;
const rescueOffered = new Set();

// Stats tracking — persisted via history.json (see loadHistory/saveHistory below).
let todayStats = { date: new Date().toDateString(), count: 0, words: 0 };
let statsByDate = {};  // { "Mon May 14 2026": { count, words }, ... }

// ── Theme ──
// Returns 'dark' or 'light' based on Windows system setting (or explicit user override).
// 'light' is the Salbei world: paper and olive in every window and overlay.
function currentTheme() {
  const override = config.overlay_theme;  // 'dark' | 'light' | 'system' (or undefined)
  if (override === 'dark' || override === 'light') return override;
  return nativeTheme.shouldUseDarkColors ? 'dark' : 'light';
}

// Native parts (context menus, select popups) follow Electron's theme, so pin
// that to the user's choice: Dark on a light Windows must not get pale menus.
function syncThemeSource() {
  const o = config.overlay_theme;
  const src = o === 'dark' || o === 'light' ? o : 'system';
  if (nativeTheme.themeSource !== src) nativeTheme.themeSource = src;
}

// The Dark world lights its controls in the overlay's accent, like a neon
// tube filled with that gas. Salbei ignores it.
const GAS = {
  mint: '#00d4aa', mono: '#f4f1ea', aurora: '#ff4fb4', blue: '#409cff',
  violet: '#bf5af2', red: '#ff5a4e', amber: '#ffb340',
};
function currentGas() { return GAS[config.overlay_accent] || GAS.mint; }

// Settings and setup paint an opaque wall in both designs; the window gets
// the same colour so opening or resizing never flashes another one.
function wallColor() { return currentTheme() === 'dark' ? '#0f0e0d' : '#f3f0e6'; }

function broadcastTheme() {
  const theme = currentTheme();
  for (const w of [settingsWin, overlayWin, toastWin, onboardingWin, suggestWin, updateWin]) {
    if (w && !w.isDestroyed()) {
      try { w.webContents.send('set-theme', theme, currentGas()); } catch (_) {}
    }
  }
  for (const w of [settingsWin, onboardingWin]) {
    if (w && !w.isDestroyed()) w.setBackgroundColor(wallColor());
  }
}

// ── Config ──
function loadConfig() {
  // A broken settings.json falls back to the newest good backup (and is kept
  // as settings.json.corrupt) instead of being overwritten with defaults.
  try {
    const r = loadSettingsFile(CONFIG_FILE);
    if (r.error) console.error(`Config unreadable (${r.error})${r.corrupt ? ', kept as ' + path.basename(r.corrupt) : ''}`);
    if (r.config && r.source !== CONFIG_FILE) console.log(`Config restored from ${path.basename(r.source)}`);
    if (r.config) config = r.config;
  } catch (e) {
    console.error('Config load error:', e.message);
  }
  config = {
    language: 'de',
    model_size: 'small',
    hotkey: 'ctrl+shift+space',
    mode: 'hold_to_speak',
    autostart: false,
    update_check: true,
    auto_language_detect: true,
    spoken_punctuation: true,
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
    agent_enabled: false,
    agent_hotkey: 'ctrl+shift+alt+space',
    agent_url: '',
    agent_token: '',
    agent_send_context: true,
    learn_corrections: true,
    save_history: true,
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

// ── Autostart (start with Windows) ──
// A value "NoType" under HKCU\…\CurrentVersion\Run that launches this exe.
// The registry is the truth, including the on/off switch in Task Manager;
// config.autostart only mirrors it for the settings page. A dev run
// (electron.exe + app folder) never registers: Windows would start a bare
// Electron without the app. The NSIS uninstaller removes the value.
const LOGIN_ITEM = { name: 'NoType', path: process.execPath, args: [] };

function autostartEnabled() {
  if (!IS_PACKAGED) return !!config.autostart;
  try {
    return app.getLoginItemSettings({ path: LOGIN_ITEM.path, args: LOGIN_ITEM.args }).executableWillLaunchAtLogin;
  } catch (e) {
    console.error('Autostart read failed:', e.message);
    return !!config.autostart;
  }
}

function setAutostart(on) {
  on = !!on;
  if (IS_PACKAGED) {
    try {
      // enabled: true also clears a "disabled" flag Task Manager may have set.
      app.setLoginItemSettings({ ...LOGIN_ITEM, openAtLogin: on, enabled: true });
    } catch (e) {
      console.error('Autostart write failed:', e.message);
    }
  }
  config.autostart = on;
  console.log(`Autostart ${on ? 'on' : 'off'}${IS_PACKAGED ? '' : ' (dev run: registry untouched)'}`);
}

// Config as the settings page should see it: autostart from the registry.
function configForSettings(cfg) {
  return { ...cfg, autostart: autostartEnabled() };
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

  const startedAt = Date.now();   // its uptime decides what an exit counts as
  let spawnError = null;          // 'ENOENT' …: it never ran ('error' comes before 'close')
  try {
    pythonProcess = spawn(cmd, args, {
      cwd: ROOT_DIR,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    });
  } catch (err) {
    // Node only reports ENOENT, EACCES and a few more as 'error' below;
    // the rest it throws, among them EPERM and UNKNOWN, which is what a
    // backend.exe blocked by a virus scanner or an app control policy gives.
    // Uncaught, that ended the startup (no hotkeys, tray on "loading") or,
    // from a restart, the main process. Now it is a failed start like ENOENT.
    console.error('Backend spawn error:', err);
    pythonProcess = null;
    setImmediate(() => backendEnded(null, startedAt, err.code || 'UNKNOWN'));
    return;
  }
  // A fresh backend gets the full watchdog grace, not the dead one's clock.
  lastPongTs = Date.now();
  missedPings = 0;
  // Writing to a backend that just died fails asynchronously (EPIPE); without
  // a listener that 'error' would be an uncaught exception in the main process.
  pythonProcess.stdin.on('error', (err) => {
    console.error('Backend stdin error:', err.code || err.message);
  });

  let buffer = '';
  // Decode as a stream: a chunk can end in the middle of a multi-byte
  // character (umlauts), which per-chunk toString() turns into U+FFFD.
  pythonProcess.stdout.setEncoding('utf8');
  pythonProcess.stdout.on('data', (data) => {
    buffer += data;
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (line.trim()) handlePythonMessage(line.trim());
    }
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error('Python:', data.toString().trim());
  });

  pythonProcess.on('close', (code) => backendEnded(code, startedAt, spawnError));

  pythonProcess.on('error', (err) => {
    console.error('Backend spawn error:', err);
    // Only a start that failed (not e.g. a kill that failed later on).
    if (String(err.syscall || '').startsWith('spawn')) spawnError = err.code || 'UNKNOWN';
  });

  // Reset the breaker once the backend has run cleanly for 30s – a transient
  // one-off crash shouldn't count toward the give-up threshold forever.
  backendUptimeTimer = setTimeout(() => {
    if (pythonProcess) { consecutiveFastCrashes = 0; crashToastShown = false; }
  }, 30_000);

  pythonStartCount++;
}

// The backend ended, or never started (`spawnError`, then `code` is the
// negative errno or null): restart it, or give up after a crash loop.
function backendEnded(code, startedAt, spawnError) {
  console.log(`Python exited (code ${code})`);
  pythonProcess = null;
  modelLoaded = false;
  modelFailure = null;   // a new backend loads again
  rescueRunning = null;
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
  // settling. Count backends that die within 10 s of their start; after 5
  // in a row, STOP and require a manual restart. A single watchdog-kill
  // (uptime >65s) resets the counter, so it's never mistaken for a crash
  // loop (lib/backend-breaker.js).
  const breaker = afterExit(consecutiveFastCrashes, Date.now() - startedAt);
  consecutiveFastCrashes = breaker.fastCrashes;

  if (breaker.giveUp) {
    backendGaveUp = true;
    console.error('Backend crash-looping – giving up auto-restart');
    // Not "loading" for good: tray, settings and hotkey say it, and "Try
    // again" starts the backend once more (retryModelLoad). A missing or
    // blocked backend.exe or DLL, or a processor without AVX, is worded
    // as such instead of the generic code: 'backend'.
    const failure = backendFailure(code, spawnError);
    // Always a toast here: the crash toast of the first crash (which
    // blocked this one before) promised a restart that is not coming.
    if (failure.code === 'backend') showToast(trayT().backendDead, 'error', null, 'restart');
    crashToastShown = true;
    setModelFailed(failure, { toast: failure.code !== 'backend' });
    return;  // require manual restart – stop hammering the GPU
  }

  // Exponential backoff for fast crashes: 2s, 4s, 8s, 16s; else 2s.
  const delay = breaker.delay;
  if (!crashToastShown) {
    showToast(trayT().backendCrashed, 'error');
    crashToastShown = true;
  }
  console.log(`Restarting backend in ${delay} ms (fast-crash streak ${consecutiveFastCrashes})`);
  setTimeout(() => { if (!quitRequested && !backendGaveUp) startPython(); }, delay);
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

// Parsing and handling are kept apart so that neither a stray non-JSON line
// nor a bug in a handler ends up writing the line, and with it dictated text,
// to the log. Only the message type and the error are logged.
function handlePythonMessage(line) {
  const msg = parseBackendLine(line);
  if (msg.error) {
    console.log(`Backend output ignored: ${msg.error}`);
    return;
  }
  const { type, data } = msg;
  try {
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
        if (isRecording && (config.mode || 'hold_to_speak') === 'hold_to_speak') stopRecording();
        break;

      case 'escape_pressed':
        // Esc (swallowed by the backend's keyboard hook) during a dictation.
        if (isRecording) cancelRecording();
        else if (isTranscribing) {
          // The backend skips the insertion and answers with a cancelled
          // transcription_done; the overlay can go right away.
          if (overlayWin && !overlayWin.isDestroyed()) overlayWin.webContents.send('recording-cancelled');
          hideOverlay();
        }
        break;

      case 'attachment_added':
        // A copy that goes along with the dictation (AI context) → overlay chip.
        if (!data || !(isRecording || isTranscribing)) break;
        attachments.push(data);
        if (overlayReady && overlayWin && !overlayWin.isDestroyed()) overlayWin.webContents.send('attachment', data);
        break;

      case 'mic_fallback': {
        const [micTitle, micSub] = trayT().micFallback;
        showToast([micTitle.replace('{name}', (data && data.name) || ''), micSub], 'warning', null, 'settings:system');
        break;
      }

      case 'amplitude':
        if (data && overlayWin && !overlayWin.isDestroyed()) {
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
        armPipelineGuard();
        updateTray('processing');
        break;

      case 'transcribe_status':
        // Something slow is going on (long audio, a slow processor, the engine
        // restarting): the overlay says what, and the guard knows it's alive.
        if (!isTranscribing) break;
        armPipelineGuard();
        if (overlayWin && !overlayWin.isDestroyed()) overlayWin.webContents.send('transcribe-status', data);
        break;

      case 'transcription_failed':
        // All tries failed. The audio is safe, if there was a copy of it.
        finishTranscription();
        sendToOnboarding('transcription-done', { text: '', empty: true, failed: true });
        if (data && data.saved) {
          rescueOffered.add(data.saved);
          showToast(trayT().transcribeFailedSaved, 'error', null, `rescue:${data.saved}`);
        } else {
          showToast(trayT().transcribeFailed, 'error');
        }
        break;

      case 'engine_fallback':
        // Not enough graphics memory is no GPU that stopped answering.
        showToast(data && data.code === 'memory' ? trayT().gpuFallbackMemory : trayT().gpuFallback, 'warning');
        break;

      case 'rescue_available':
        rescueItems = (data && data.items) || [];
        rescueDir = (data && data.dir) || '';
        if (data && data.startup) rescueOfferPending = true;
        updateTray(trayStatus);
        break;

      case 'rescue_done': {
        rescueRunning = null;
        const status = trayStatus;
        if (data && data.text) {
          addToHistory(data.text);
          trackStats(data.text);
          showToast(trayT().rescueDone, 'warning');
        } else {
          showToast(trayT().rescueEmpty, 'warning');
        }
        updateTray(status);
        break;
      }

      case 'rescue_failed':
        rescueRunning = null;
        showToast(trayT().rescueFailed, 'error', null, data && data.id ? `rescue:${data.id}` : null);
        updateTray(trayStatus);
        break;

      case 'preview':
        // Rolling live transcript while recording – overlay caption only.
        if (data && overlayWin && !overlayWin.isDestroyed() && isRecording) {
          overlayWin.webContents.send('preview-text', data.text);
        }
        break;

      case 'dictionary_suggestions':
        if (config.learn_corrections !== false) showSuggestion(data && data.items);
        break;

      case 'transcription_done':
        // `clipboard`: the user moved on to another window meanwhile, so the
        // text wasn't pasted. No "inserted" on the overlay then.
        finishTranscription(!!(data && data.text && !data.empty && !data.clipboard), data && data.text);
        sendToOnboarding('transcription-done', data || {});
        if (data && data.command) {
          // Command mode: the result is LLM output, not a dictation – it
          // stays out of the history (that's for learning recognition errors).
          if (data.reason === 'command_no_llm') showToast(trayT().commandNoLlm, 'warning', null, 'settings:cleanup');
          else if (data.reason === 'command_failed') showToast(trayT().commandFailed, 'error');
          else if (data.reason === 'too_short') showToast(trayT().tooShort, 'warning');
        } else if (data && data.text && !data.empty) {
          addToHistory(data.text);
          trackStats(data.text);
          // Success popup is opt-in – the inserted text IS the feedback.
          // Warnings and errors always show.
          if (data.clipboard) {
            showToast(trayT().clipboardMoved, 'warning');
          } else if (config.success_toast === true) {
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

      case 'agent_sending':
        // Agent hotkey: transcribed, now on its way to the URL. Nothing gets
        // pasted, so the hotkeys can come back already.
        if (!(isTranscribing && agentActive)) break;
        registerHotkeys();
        if (overlayWin && !overlayWin.isDestroyed()) {
          const text = config.live_preview_enabled !== false && data && data.text ? String(data.text) : '';
          overlayWin.webContents.send('agent-sending', { text });
        }
        break;

      case 'agent_result':
        if (data && data.test) {
          if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('agent-test-result', data);
        } else if (isTranscribing && agentActive) {
          finishAgent(data || {});
        } else {
          console.log('Agent answer without a running dictation, dropped');
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

      case 'model_loading':
        // A (re)load started: whatever failed before is being tried again,
        // and a success after a failure gets its "ready" toast.
        if (modelFailure && !pendingModelSwitch) pendingModelSwitch = (data && data.model) || config.model_size;
        modelLoaded = false;
        modelFailure = null;
        if (!isRecording && !isTranscribing) updateTray('loading');
        if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('model-loading', data || {});
        sendToOnboarding('model-loading', data || {});
        break;

      case 'model_failed':
        setModelFailed(data);
        break;

      case 'model_ready':
        modelLoaded = true;
        modelInfo = data;
        const recovered = !!modelFailure;
        modelFailure = null;
        console.log(`✓ Model: ${data.model} on ${data.device}`);
        // The engine may have been replaced in the middle of a dictation.
        updateTray(isRecording || isTranscribing ? trayStatus : 'ready');
        const rescueOffer = offerRescue();
        if (pendingModelSwitch || recovered) {
          showToast(trayT().modelReady.replace('{model}', modelLabel(data.model)), 'success');
          pendingModelSwitch = null;
        } else if (!bootReadyToastShown && !rescueOffer) {
          // Once per launch – answers "is it ready yet?" on slower machines.
          const dev = String(data.device || '').split(' (')[0];
          showToast(JUST_UPDATED
            ? trayT().bootUpdated.replace('{v}', app.getVersion())
            : trayT().bootReady.replace('{model}', modelLabel(data.model)).replace('{device}', dev), 'success');
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
          const prevKeys = hotkeySignature(config);
          config = data;
          if (normalizeOverlayConfig()) writeConfigToDisk();
          if (hotkeySignature(data) !== prevKeys) registerHotkeys();
        }
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('config', configForSettings(data));
        }
        break;

      case 'config_saved':
        config = data;
        normalizeOverlayConfig();
        registerHotkeys();
        break;

      case 'status':
        console.log('Status:', data && data.message);
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

      case 'error': {
        const message = String((data && data.message) || '');
        console.error('Backend error:', message);
        // A model that won't load says nothing about the running dictation:
        // the engine tries again for it and reports how that ended. Normally
        // model_failed came right before and said it all; a backend that
        // only sends this still gets the failure state.
        if (/^Model load failed/.test(message)) {
          if (!modelFailure) setModelFailed({ message: message.replace(/^Model load failed:\s*/, '') });
          break;
        }
        // CRITICAL: if an error arrives while we were mid-transcription,
        // restore the state machine so the hotkey works again. Without this
        // a single transcription error locks the app dead.
        if (isRecording || isTranscribing) finishTranscription();
        showBackendError(message);
        break;
      }
    }
  } catch (e) {
    console.error(`Handling backend message '${type}' failed:`, e && e.message);
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
  slime:  { w: 640, h: 112, bottom: 8,  top: 0 },   // blob 104×92 + bubble 500×50
  dragon: { w: 612, h: 100, bottom: 8,  top: 0 },   // pixel dragon 136×80, bubble 476×50 tucked under its fire
  mimic:  { w: 666, h: 116, bottom: 8,  top: 0 },   // pixel chest 128×96 + bubble 500×50
  line:   { w: 700, h: 96,  bottom: 10, top: 0 },   // pill 660×60 + its drop shadow (Salbei)
};
const DEFAULT_OVERLAY_STYLE = 'amoled';
const OVERLAY_POSITIONS = ['bottom', 'top', 'center'];
// Room for the AI context chips: above the shape, below it when the overlay
// sits at the top. The shape itself keeps its place on screen.
const CHIP_STRIP = 40;
const OVERLAY_ACCENTS = ['mint', 'mono', 'aurora', 'blue', 'violet', 'red', 'amber'];
// Retired style ids → [successor, position]. Pre-2.6 the AMOLED placement was
// baked into the id; 2.7 replaced the classic visualizers (and dropped the Orb);
// in 3.1 the light Sage style became Line, which exists in both worlds.
const LEGACY_OVERLAY_STYLES = {
  sage:          ['line', null],
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
  const strip = chipStrip();

  overlayWin.setBounds({ width: s.w, height: s.h + strip, x: 0, y: 0 });

  const posX = x + Math.round((width - s.w) / 2);
  let posY;
  switch (config.overlay_position) {
    case 'top':    posY = y + s.top; break;
    case 'center': posY = y + Math.round((height - s.h) / 2) - strip; break;
    case 'bottom':
    default:       posY = y + height - s.h - strip - s.bottom; break;
  }
  overlayWin.setPosition(posX, posY);
}

function chipStrip() {
  return config.ai_context !== false ? CHIP_STRIP : 0;
}

function createOverlay() {
  const initial = getStyleConfig(config.overlay_style);
  overlayWin = new BrowserWindow({
    width: initial.w,
    height: initial.h + chipStrip(),
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
      sandbox: true,
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
// so the IPC messages are never dropped. `agentHint`: show that agent result
// instead of starting a recording (agent hotkey without a URL).
function pushOverlayState(agentHint = null) {
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
      strip: chipStrip() > 0,
    });
    overlayWin.webContents.send('set-theme', currentTheme(), currentGas());
    if (agentHint) { overlayWin.webContents.send('agent-result', agentHint); return; }
    overlayWin.webContents.send('recording-started', { command: commandActive, agent: agentActive });
    // Released before the window had loaded: that message went nowhere, and
    // the overlay would say "listening" for the whole transcription.
    if (isTranscribing) overlayWin.webContents.send('recording-stopped');
    // Copies that came in before the window had loaded (look-back at start).
    for (const a of attachments) overlayWin.webContents.send('attachment', a);
  };
  if (overlayReady) send();
  else overlayWin.webContents.once('did-finish-load', send);
}

let overlayHideTimer = null;

function showOverlay(agentHint = null) {
  if (overlayIdleTimer) { clearTimeout(overlayIdleTimer); overlayIdleTimer = null; }
  if (overlayHideTimer) { clearTimeout(overlayHideTimer); overlayHideTimer = null; }
  if (!overlayWin || overlayWin.isDestroyed()) createOverlay();
  applyOverlayBounds();
  overlayWin.showInactive();
  pushOverlayState(agentHint);
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

// Everything registerHotkeys() depends on: re-register when it changes.
function hotkeySignature(cfg) {
  return [cfg.hotkey, cfg.mode, cfg.command_enabled, cfg.command_hotkey,
          cfg.agent_enabled, cfg.agent_hotkey].join('|');
}

// Same keys in any order ("Alt+Shift+X" = "Shift+Alt+X").
function sameAccelerator(a, b) {
  const norm = (k) => String(k || '').toLowerCase().split('+').sort().join('+');
  return norm(a) === norm(b);
}

// What the running recording is for: 'dictate', 'command' or 'agent'.
function activeKind() {
  return commandActive ? 'command' : agentActive ? 'agent' : 'dictate';
}

let holdTimer = null;  // Timer for hold-to-speak release detection

// One handler for every hotkey: dictation, command mode and the agent
// hotkey (the text goes to a URL instead of being typed).
function onHotkey(kind) {
  const mode = config.mode || 'hold_to_speak';
  const keys = kind === 'command' ? config.command_hotkey
    : kind === 'agent' ? config.agent_hotkey : config.hotkey;
  if (mode === 'hold_to_speak') {
    // Cancelled with Esc while the hotkey is still held: its key repeats
    // must not start a new recording. Every repeat extends the quiet time,
    // so it ends shortly after the keys are let go.
    if (Date.now() < hotkeyQuietUntil) {
      hotkeyQuietUntil = Date.now() + 700;
      return;
    }
    // Hold-to-speak: globalShortcut fires repeatedly while held.
    // Start recording on first fire, reset release-detection timer each fire.
    if (!isRecording && !isTranscribing) {
      startRecording({ kind });
      // Precise release: the backend polls the hotkey's virtual keys
      // (GetAsyncKeyState, ~15 ms) and sends 'hotkey_released'.
      // globalShortcut has no key-up event – the old 350 ms-after-last-
      // repeat timer added 350 ms to every dictation and, when the key
      // was held shorter than the OS repeat delay (~500 ms), fired before
      // the first repeat and cut the recording off mid-word.
      if (isRecording) sendToPython('watch_hotkey_release', { hotkey: keys });
    }
    // The other hotkeys' repeats must not keep this recording alive.
    if (isRecording && activeKind() !== kind) return;
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
      startRecording({ kind });
    } else {
      stopRecording();
    }
  }
}

// registerHotkeys() runs after every dictation: tell about a taken hotkey
// once, not each time (again only after it worked in between).
let hotkeyWarned = null;
function warnHotkey(hotkey, fallbackOk, invalid) {
  if (hotkeyWarned === hotkey) return;
  hotkeyWarned = hotkey;
  const T = trayT();
  const [taken, sub] = fallbackOk ? T.hotkeyFallback : T.hotkeyFailed;
  const title = invalid ? T.hotkeyInvalid : taken;
  showToast([title.replace('{key}', config.hotkey || hotkey), sub], fallbackOk ? 'warning' : 'error', null, 'settings:general');
}

function registerHotkeys() {
  globalShortcut.unregisterAll();
  if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }

  const hotkey = configHotkeyToElectron(config.hotkey);
  const mode = config.mode || 'hold_to_speak';
  console.log(`Registering hotkey: ${hotkey} (mode: ${mode})`);

  // register() throws on an accelerator Electron can't parse (invalid).
  let success = false;
  let invalid = false;
  try {
    success = globalShortcut.register(hotkey, () => onHotkey('dictate'));
  } catch (e) {
    console.error('Hotkey registration error:', e);
    invalid = true;
  }

  if (!success) {
    console.error(`✗ Failed to register hotkey: ${hotkey}`);
    let fallbackOk = false;
    try {
      fallbackOk = globalShortcut.register('CmdOrCtrl+Shift+Space', () => {
        if (!isRecording) startRecording();
        else stopRecording();
      });
    } catch (e) {
      console.error('Fallback hotkey error:', e);
    }
    console.log(fallbackOk ? 'Using fallback: CmdOrCtrl+Shift+Space' : '✗ Fallback hotkey taken too');
    warnHotkey(hotkey, fallbackOk, invalid);
  } else {
    console.log(`✓ Hotkey registered: ${hotkey} (${mode})`);
    hotkeyWarned = null;
  }

  // Command mode: select text, hold the second hotkey, speak an instruction.
  if (config.command_enabled !== false && config.command_hotkey) {
    const cmdKey = configHotkeyToElectron(config.command_hotkey);
    try {
      if (cmdKey.toLowerCase() === hotkey.toLowerCase()) {
        console.error(`✗ Command hotkey equals the dictation hotkey (${cmdKey}) – skipped`);
      } else if (globalShortcut.register(cmdKey, () => onHotkey('command'))) {
        console.log(`✓ Command hotkey registered: ${cmdKey}`);
      } else {
        console.error(`✗ Failed to register command hotkey: ${cmdKey}`);
      }
    } catch (e) {
      console.error('Command hotkey registration error:', e);
    }
  }

  // Agent hotkey (off by default): the dictation goes to a URL instead.
  if (config.agent_enabled === true && config.agent_hotkey) {
    const agentKey = configHotkeyToElectron(config.agent_hotkey);
    const taken = [hotkey];
    if (config.command_enabled !== false && config.command_hotkey) taken.push(configHotkeyToElectron(config.command_hotkey));
    try {
      if (taken.some((k) => sameAccelerator(k, agentKey))) {
        console.error(`✗ Agent hotkey equals another hotkey (${agentKey}), skipped`);
      } else if (globalShortcut.register(agentKey, () => onHotkey('agent'))) {
        console.log(`✓ Agent hotkey registered: ${agentKey}`);
      } else {
        console.error(`✗ Failed to register agent hotkey: ${agentKey}`);
      }
    } catch (e) {
      console.error('Agent hotkey registration error:', e);
    }
  }
}

function startRecording({ kind = 'dictate' } = {}) {
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
  // Agent hotkey without a URL: say so instead of recording for nothing.
  if (kind === 'agent' && !String(config.agent_url || '').trim()) {
    showAgentHint();
    return;
  }
  if (!modelLoaded) {
    // One toast per press: hold mode repeats the hotkey ~30×/s, and every
    // repeat extends the quiet time. Failed is not "still loading": then
    // the toast says why and where to fix it.
    const now = Date.now();
    if (now >= modelHintUntil) {
      if (modelFailure) showModelFailure();
      else showToast(trayT().modelNotReady, 'warning');
    }
    modelHintUntil = now + 700;
    return;
  }
  isRecording = true;
  commandActive = kind === 'command';
  agentActive = kind === 'agent';
  attachments = [];
  closeSuggestion();  // a new dictation supersedes it (and the overlay sits there)
  sendToOnboarding('recording-started');
  isPaused = false;
  showOverlay();
  // Command mode works on the selection; the AI context stays out of it
  // (and out of the agent hotkey, which only sends app and window title).
  sendToPython('start_recording', commandActive ? { command: true } : agentActive ? { agent: true } : undefined);
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
  if (agentActive) sendToPython('stop_agent', { version: app.getVersion() });
  else sendToPython(commandActive ? 'stop_command' : 'stop_recording');
  updateTray('processing');
  armPipelineGuard();
}

// How long a transcription takes is no failure: on a processor, minutes of
// audio take minutes, and the backend reports about once a second meanwhile.
// Only silence counts. The longest quiet stretch in a healthy pipeline is an
// answer from the local AI or the agent URL (under a minute). Killing the
// backend is safe, the next one finds the audio on disk and offers it.
const PIPELINE_SILENCE_MS = 180_000;
let pipelineGuard = null;
function armPipelineGuard() {
  if (pipelineGuard) clearTimeout(pipelineGuard);
  pipelineGuard = setTimeout(() => {
    pipelineGuard = null;
    if (!isTranscribing) return;
    console.error('Backend silent during a dictation – restarting it');
    if (pythonProcess) {
      try { pythonProcess.kill('SIGKILL'); } catch (_) {}
      return;  // the close handler resets everything
    }
    finishTranscription();
    showToast(trayT().transcribeFailed, 'error');
  }, PIPELINE_SILENCE_MS);
}
function clearPipelineGuard() {
  if (pipelineGuard) clearTimeout(pipelineGuard);
  pipelineGuard = null;
}

// Esc while recording: the take is dropped, nothing is transcribed or inserted.
function cancelRecording() {
  if (!isRecording) return;
  isRecording = false;
  isPaused = false;
  if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
  if ((config.mode || 'hold_to_speak') === 'hold_to_speak') hotkeyQuietUntil = Date.now() + 700;
  sendToPython('cancel_recording');
  if (overlayWin && !overlayWin.isDestroyed()) overlayWin.webContents.send('recording-cancelled');
  sendToOnboarding('transcription-done', { text: '', empty: true, cancelled: true });
  hideOverlay();
  registerHotkeys();
  updateTray('ready');
}

// Single place to reset post-recording state. Called from `transcription_done`,
// `transcription_failed`, `error`, and when the backend ended or fell silent.
function finishTranscription(ok = false, text = '') {
  clearPipelineGuard();
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

// Agent hotkey: how long the overlay shows a result. Long enough to read a
// reply (it rolls through the line in the overlay), short when there is none.
function agentHoldMs(result) {
  if (!result.ok) return 3600;
  const n = String(result.message || '').length;
  return n ? Math.min(15_000, 2600 + n * 60) : 1400;
}

function agentOverlayData(result, hold) {
  const ok = !!result.ok;
  return {
    ok,
    text: ok ? String(result.message || '') : '',
    error: ok ? '' : String(result.error || 'unreachable'),
    status: Number(result.status) || null,
    hold,
  };
}

// The URL answered (or didn't): show it, then back to ready. Never types
// anything, also not as a fallback.
function finishAgent(result) {
  clearPipelineGuard();
  isRecording = false;
  isTranscribing = false;
  isPaused = false;
  const hold = agentHoldMs(result);
  if (overlayWin && !overlayWin.isDestroyed()) {
    overlayWin.webContents.send('agent-result', agentOverlayData(result, hold));
  }
  setTimeout(() => { if (!isRecording) hideOverlay(); }, hold);
  registerHotkeys();
  updateTray('ready');
}

// Agent hotkey pressed, but no URL set: a short hint on the overlay.
function showAgentHint() {
  const now = Date.now();
  if (now < agentHintUntil) return;   // hold mode repeats the hotkey ~30×/s
  const result = { ok: false, error: 'no_url' };
  const hold = agentHoldMs(result);
  agentHintUntil = now + hold;
  showOverlay({ ...agentOverlayData(result, hold), fresh: true });
  setTimeout(() => { if (!isRecording) hideOverlay(); }, hold);
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

function createTrayImage(color = '#00d4aa', dot = false) {
  const base = getTrayBaseImage();
  if (!dot) return base;

  // Recording (red) and a model that failed to load (amber): a dot in the
  // upper-right corner of the base PNG. `toBitmap()` returns BGRA on Windows;
  // `createFromBuffer` expects the same layout when given a width+height
  // descriptor – so we write BGRA and don't swap channels. (Bugfix: previous
  // version produced a blue dot.)
  const s = 32;
  const buf = Buffer.from(base.toBitmap());
  const [red, green, blue] = [1, 3, 5].map((i) => parseInt(color.slice(i, i + 2), 16));
  const cx = s - 6, cy = 6, r = 4;
  for (let y = Math.max(0, cy - r); y < Math.min(s, cy + r + 1); y++) {
    for (let x = Math.max(0, cx - r); x < Math.min(s, cx + r + 1); x++) {
      const dx = x - cx, dy = y - cy;
      if (dx * dx + dy * dy <= r * r) {
        const i = (y * s + x) * 4;
        buf[i]     = blue;
        buf[i + 1] = green;
        buf[i + 2] = red;
        buf[i + 3] = 255;  // A
      }
    }
  }
  return nativeImage.createFromBuffer(buf, { width: s, height: s });
}

function createTray() {
  const icon = createTrayImage('#00d4aa');
  tray = new Tray(icon);
  tray.setToolTip('NoType · Bereit');
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

let trayStatus = 'ready';   // last status shown, so the menu can be rebuilt as-is
function updateTray(status) {
  if (!tray) return;
  trayStatus = status;
  const colors = { ready: '#00d4aa', recording: '#ef4444', processing: '#ffaa00', loading: '#888888', paused: '#ffaa00', failed: '#f59e0b' };
  const T = trayT();
  const labels = {
    ready: T.ready,
    recording: T.recording,
    processing: T.processing,
    loading: T.loading,
    paused: T.paused,
    failed: T.failed,
  };

  // Override status if model not loaded: still loading, or it failed
  if (!modelLoaded && status === 'ready') status = modelFailure ? 'failed' : 'loading';

  try {
    tray.setImage(createTrayImage(colors[status] || '#00d4aa', status === 'recording' || status === 'failed'));
    const hotkeyLabel = config.hotkey || 'Ctrl+Shift+Space';
    const upd = update.state === 'downloading' ? ` · ${T.updLoading.replace('{p}', update.percent)}` : '';
    tray.setToolTip(`NoType · ${labels[status] || T.ready} (${hotkeyLabel})${upd}`);
    tray.setContextMenu(buildTrayMenu(status));
  } catch (e) {
    console.error('Tray update error:', e);
  }
}

// Minimal tray/toast i18n – the settings window has its own dictionary.
function trayT() {
  const de = {
    ready: '✓ Bereit', recording: '● Aufnahme …', processing: '⟳ Transkribiere …',
    loading: '⏳ Modell lädt …', paused: '⏸ Pausiert',
    failed: '⚠ Modell nicht geladen', failedOpen: 'Sprache & Modell öffnen …', failedRetry: '↻  Erneut versuchen',
    today: 'Heute', words: 'Wörter', transcriptions: 'Transkriptionen',
    last7: 'Letzte 7 Tage', last30: 'Letzte 30 Tage',
    history: 'Letzte Transkriptionen', settings: '⚙  Einstellungen', setup: '✦  Einrichtung', autostart: 'Mit Windows starten', quit: '✕  Beenden',
    // Warnings and errors: [what happened, what to do now].
    tooShort: ['Aufnahme zu kurz', 'Nichts eingefügt. Einfach etwas länger sprechen.'],
    backendCrashed: ['Spracherkennung abgestürzt', 'NoType startet sie gerade neu. Gleich geht es weiter.'],
    backendDead: ['Spracherkennung startet nicht', 'Mehrere Versuche sind gescheitert. NoType einmal neu starten.'],
    modelLoading: 'Lade Modell {model} …', modelReady: '{model} ist bereit',
    modelNotReady: ['Modell lädt noch', 'In ein paar Sekunden noch einmal versuchen.'],
    transcribeFailed: ['Diktat ließ sich nicht umwandeln', 'Bitte noch einmal diktieren.'],
    transcribeFailedSaved: ['Diktat ließ sich gerade nicht umwandeln', 'Die Aufnahme ist gespeichert, nichts ist verloren.'],
    clipboardMoved: ['Fenster gewechselt, nichts eingefügt', 'Der Text liegt in der Zwischenablage. Mit Strg+V einfügen.'],
    gpuFallback: ['Grafikkarte antwortet nicht', 'NoType arbeitet auf dem Prozessor weiter und wechselt von selbst zurück.'],
    gpuFallbackMemory: ['Zu wenig Grafikspeicher für das Modell', 'NoType arbeitet auf dem Prozessor weiter. Ein kleineres Modell passt auf die Grafikkarte.'],
    rescueFound: ['Aufnahme von {when} ist noch nicht umgewandelt', '{dur} gesprochen. Der Text kommt in die Zwischenablage.'],
    rescueStarted: 'Aufnahme wird umgewandelt …',
    rescueDone: ['Aufnahme umgewandelt', 'Der Text liegt in der Zwischenablage. Mit Strg+V einfügen.'],
    rescueEmpty: ['Keine Sprache in der Aufnahme', 'Sie wurde gelöscht.'],
    rescueFailed: ['Aufnahme ließ sich wieder nicht umwandeln', 'Sie bleibt gespeichert und wartet im Tray-Menü.'],
    rescueMenu: 'Gespeicherte Aufnahmen ({n})', rescueBusy: 'wird umgewandelt …',
    rescueOpen: 'Ordner öffnen', rescueDeleteAll: 'Alle löschen', actRescue: 'Umwandeln',
    devicesFailed: ['Mikrofone nicht lesbar', 'Mikrofon-Verbindung prüfen und die Einstellungen neu öffnen.'],
    unknownError: 'Etwas ist schiefgelaufen',
    bootReady: 'NoType bereit · {model} · {device}',
    commandNoLlm: ['Befehlsmodus braucht die lokale KI', 'Unter „Text-Bereinigung“ einrichten.'],
    commandFailed: ['Befehl ohne Antwort', 'Die lokale KI hat nicht rechtzeitig geantwortet. Noch einmal versuchen.'],
    bootUpdated: 'Auf {v} aktualisiert, NoType ist bereit',
    micFallback: ['Mikrofon „{name}“ nicht gefunden', 'NoType nimmt mit dem Standardmikrofon auf.'],
    hotkeyFallback: ['Hotkey {key} ist schon belegt', 'Diktieren geht vorerst mit Strg+Umschalt+Leertaste: einmal drücken startet, nochmal drücken stoppt. Unter „Allgemein“ einen anderen wählen.'],
    hotkeyFailed: ['Hotkey {key} ist schon belegt', 'Diktieren per Hotkey geht gerade nicht. Unter „Allgemein“ einen anderen wählen.'],
    hotkeyInvalid: 'Hotkey {key} wird nicht erkannt',
    actRestart: 'Neu starten', actSetup: 'Einrichten', actOpen: 'Öffnen', actChange: 'Ändern',
    actRetry: 'Erneut versuchen',
    updCheck: 'Nach Updates suchen', updChecking: 'Suche nach Updates …',
    updInstall: '↑  Auf {v} aktualisieren', updDownload: '↑  NoType {v} herunterladen',
    updLoading: 'Update wird geladen: {p} %', updInstalling: 'Update wird installiert …',
    updCurrent: 'NoType {v} ist aktuell', updFailed: ['Keine Verbindung zu GitHub', 'Später noch einmal nach Updates suchen.'],
    locale: 'de-DE',
  };
  const en = {
    ready: '✓ Ready', recording: '● Recording …', processing: '⟳ Transcribing …',
    loading: '⏳ Loading model …', paused: '⏸ Paused',
    failed: '⚠ Model not loaded', failedOpen: 'Open Language & Model …', failedRetry: '↻  Try again',
    today: 'Today', words: 'words', transcriptions: 'transcriptions',
    last7: 'Last 7 days', last30: 'Last 30 days',
    history: 'Recent transcriptions', settings: '⚙  Settings', setup: '✦  Setup', autostart: 'Start with Windows', quit: '✕  Quit',
    tooShort: ['Recording too short', 'Nothing was inserted. Just speak a little longer.'],
    backendCrashed: ['Speech recognition crashed', 'NoType is restarting it. Back in a moment.'],
    backendDead: ['Speech recognition won’t start', 'Several attempts failed. Restart NoType once.'],
    modelLoading: 'Loading model {model} …', modelReady: '{model} is ready',
    modelNotReady: ['Model still loading', 'Try again in a few seconds.'],
    transcribeFailed: ['Dictation could not be transcribed', 'Please dictate again.'],
    transcribeFailedSaved: ['Dictation could not be transcribed right now', 'The recording is saved, nothing is lost.'],
    clipboardMoved: ['Window changed, nothing inserted', 'The text is on the clipboard. Paste it with Ctrl+V.'],
    gpuFallback: ['Graphics card is not responding', 'NoType keeps working on the processor and switches back by itself.'],
    gpuFallbackMemory: ['Not enough graphics memory for the model', 'NoType keeps working on the processor. A smaller model fits on the graphics card.'],
    rescueFound: ['Recording from {when} is not transcribed yet', '{dur} of speech. The text goes to the clipboard.'],
    rescueStarted: 'Transcribing the recording …',
    rescueDone: ['Recording transcribed', 'The text is on the clipboard. Paste it with Ctrl+V.'],
    rescueEmpty: ['No speech in the recording', 'It was deleted.'],
    rescueFailed: ['Recording still could not be transcribed', 'It stays saved and waits in the tray menu.'],
    rescueMenu: 'Saved recordings ({n})', rescueBusy: 'transcribing …',
    rescueOpen: 'Open folder', rescueDeleteAll: 'Delete all', actRescue: 'Transcribe',
    devicesFailed: ['Could not list microphones', 'Check the microphone connection and reopen settings.'],
    unknownError: 'Something went wrong',
    bootReady: 'NoType ready · {model} · {device}',
    commandNoLlm: ['Command mode needs the local AI', 'Set it up under “Text Cleanup”.'],
    commandFailed: ['Command got no answer', 'The local AI did not answer in time. Try again.'],
    bootUpdated: 'Updated to {v}, NoType is ready',
    micFallback: ['Microphone “{name}” not found', 'NoType is recording with the default microphone.'],
    hotkeyFallback: ['Hotkey {key} is already taken', 'Dictate with Ctrl+Shift+Space for now: press once to start, again to stop. Pick another one under “General”.'],
    hotkeyFailed: ['Hotkey {key} is already taken', 'Dictating by hotkey does not work right now. Pick another one under “General”.'],
    hotkeyInvalid: 'Hotkey {key} is not recognized',
    actRestart: 'Restart', actSetup: 'Set up', actOpen: 'Open', actChange: 'Change',
    actRetry: 'Try again',
    updCheck: 'Check for updates', updChecking: 'Checking for updates …',
    updInstall: '↑  Update to {v}', updDownload: '↑  Download NoType {v}',
    updLoading: 'Downloading update: {p} %', updInstalling: 'Installing update …',
    updCurrent: 'NoType {v} is up to date', updFailed: ['Could not reach GitHub', 'Check for updates again later.'],
    locale: 'en-US',
  };
  return config.app_language === 'en' ? en : de;
}

// When a saved recording started: the time, plus the date if it isn't from today.
function rescueWhen(item) {
  const T = trayT();
  const started = new Date(item.started * 1000);
  const time = started.toLocaleTimeString(T.locale, { hour: '2-digit', minute: '2-digit' });
  if (started.toDateString() === new Date().toDateString()) return time;
  return `${started.toLocaleDateString(T.locale, { day: '2-digit', month: '2-digit' })} ${time}`;
}

function rescueDuration(item) {
  const s = Math.round(item.seconds);
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} min`;
}

function runRescue(id) {
  if (rescueRunning || !pythonProcess) return;  // the backend does one at a time
  rescueRunning = id;
  sendToPython('rescue_run', { id });
  showToast(trayT().rescueStarted, 'success');
  updateTray(trayStatus);
}

// What an earlier run left behind: offered once, when the model is up (the
// transcription would wait for it anyway). Older ones just wait in the tray.
function offerRescue() {
  if (!rescueOfferPending) return false;
  rescueOfferPending = false;
  const dayAgo = Date.now() - 24 * 3600 * 1000;
  const fresh = rescueItems.filter((i) => i.started * 1000 > dayAgo && !rescueOffered.has(i.id));
  fresh.forEach((i) => rescueOffered.add(i.id));
  const latest = fresh[fresh.length - 1];
  if (!latest) return false;
  const [title, sub] = trayT().rescueFound;
  showToast([title.replace('{when}', rescueWhen(latest)), sub.replace('{dur}', rescueDuration(latest))],
    'warning', null, `rescue:${latest.id}`);
  return true;
}

function buildTrayMenu(status = 'ready') {
  const T = trayT();
  const labels = {
    ready: T.ready,
    recording: T.recording,
    processing: T.processing,
    loading: T.loading,
    paused: T.paused,
    failed: T.failed,
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
    { label: `NoType · ${labels[status] || T.ready}`, enabled: false },
    ...(status === 'failed' ? [
      { label: T.failedRetry, click: () => retryModelLoad() },
      { label: T.failedOpen, click: () => openSettings('speech') },
    ] : []),
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

  // Recordings that still need transcribing
  if (rescueItems.length > 0) {
    items.push({
      label: T.rescueMenu.replace('{n}', rescueItems.length),
      submenu: [
        ...rescueItems.map((item) => ({
          label: `${rescueWhen(item)} · ${rescueDuration(item)}${item.id === rescueRunning ? ` · ${T.rescueBusy}` : ''}`,
          enabled: !rescueRunning,
          click: () => runRescue(item.id),
        })),
        { type: 'separator' },
        { label: T.rescueOpen, click: () => { if (rescueDir) shell.openPath(rescueDir); } },
        { label: T.rescueDeleteAll, click: () => sendToPython('rescue_delete') },
      ],
    }, { type: 'separator' });
  }

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
    {
      label: T.autostart, type: 'checkbox', checked: autostartEnabled(),
      click: (item) => {
        setAutostart(item.checked);
        writeConfigToDisk();
        sendToPython('save_config', { autostart: config.autostart });
        pushConfigToSettings();
      },
    },
    ...(autoUpdater ? [updateMenuItem(T)] : []),
    { type: 'separator' },
    { label: T.quit, click: () => quitApp() },
  );

  return Menu.buildFromTemplate(items);
}

// ── Settings Window ──
// pane: optional section to land on ('speech', 'cleanup', 'system', …).
function openSettings(pane) {
  if (settingsWin && !settingsWin.isDestroyed()) {
    if (pane) settingsWin.webContents.send('show-pane', pane);
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
    backgroundColor: wallColor(),
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  settingsWin.loadFile(path.join(__dirname, 'settings.html'));
  // With unsaved changes the page holds the window open and asks (its
  // beforeunload). Quitting NoType must not wait for that answer.
  settingsWin.webContents.on('will-prevent-unload', (e) => { if (quitRequested) e.preventDefault(); });
  settingsWin.once('ready-to-show', () => {
    settingsWin.webContents.send('set-theme', currentTheme(), currentGas());
    settingsWin.show();
    pushConfigToSettings();
    // Sync model status
    if (modelLoaded && modelInfo) {
      settingsWin.webContents.send('model-ready', modelInfo);
    } else if (modelFailure) {
      settingsWin.webContents.send('model-failed', modelFailure);
    }
    if (pane) settingsWin.webContents.send('show-pane', pane);
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
    backgroundColor: wallColor(),
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  onboardingWin.loadFile(path.join(__dirname, 'onboarding.html'));
  onboardingWin.once('ready-to-show', () => {
    if (!onboardingWin || onboardingWin.isDestroyed()) return;
    onboardingWin.webContents.send('set-theme', currentTheme(), currentGas());
    onboardingWin.show();
    onboardingWin.webContents.send('config', configForSettings(config));
    if (modelLoaded && modelInfo) onboardingWin.webContents.send('model-ready', modelInfo);
    else if (modelFailure) onboardingWin.webContents.send('model-failed', modelFailure);
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
  scheduleFirstUpdateCheck(config);
}

function sendToOnboarding(channel, data) {
  if (onboardingWin && !onboardingWin.isDestroyed()) {
    try { onboardingWin.webContents.send(channel, data); } catch (_) { /* window closing */ }
  }
}

// ── IPC from renderers ──
// Every channel answers only the windows of the pages that use it
// (lib/ipc-guard.js): a toast or a foreign page cannot save settings.
// Dropped messages log the channel and page name, never the payload.
const appWindows = () => ({
  'settings.html': settingsWin, 'onboarding.html': onboardingWin, 'toast.html': toastWin,
  'suggest.html': suggestWin, 'update.html': updateWin, 'overlay.html': overlayWin,
});

function onIpc(channel, handler) {
  ipcMain.on(channel, (event, ...args) => {
    const page = senderPage(event, appWindows());
    if (!allowSender(channel, page)) {
      console.warn(`IPC '${channel}' from ${page || '(not an app window)'} dropped`);
      return;
    }
    handler(event, ...args);
  });
}

// Settings need a config even when the backend is down – answer from our own
// copy then, instead of letting the form open blank.
function pushConfigToSettings() {
  if (pythonProcess && pythonProcess.stdin.writable) sendToPython('get_config');
  else if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('config', configForSettings(config));
}

onIpc('save-config', (_, input) => {
  // Known keys with a fitting type only; the log names the rest, no values.
  const { config: newConfig, rejected } = filterConfig(input);
  if (rejected.length) console.warn(`save-config: ignored ${rejected.join(', ')}`);
  if (!newConfig || !Object.keys(newConfig).length) return;
  // 1) Persist FIRST, here in the main process. This used to only forward to
  //    the backend – and sendToPython() silently drops the message while the
  //    backend is dead or in restart back-off. So a user who picked a smaller
  //    model *because* the big one kept crashing lost exactly that change.
  const prevModel = config.model_size;
  const prevKeys = hotkeySignature(config);
  const autostartChanged = typeof newConfig.autostart === 'boolean' && newConfig.autostart !== autostartEnabled();
  const prevTheme = currentTheme();
  const prevGas = currentGas();
  const prevHistory = historyEnabled();
  config = { ...config, ...newConfig };
  if (prevHistory && !historyEnabled()) saveHistory();  // drop the stored dictations now
  syncThemeSource();
  if (currentTheme() !== prevTheme || currentGas() !== prevGas) broadcastTheme();
  if (autostartChanged) {
    setAutostart(newConfig.autostart);
    updateTray(trayStatus);   // the tray menu's checkbox
  }
  writeConfigToDisk();
  if (hotkeySignature(config) !== prevKeys) registerHotkeys();
  // The setup guide's last step saves first_run_complete before it closes.
  scheduleFirstUpdateCheck(config);

  const modelChanged = !!newConfig.model_size && newConfig.model_size !== prevModel;
  if (modelChanged) {
    pendingModelSwitch = newConfig.model_size;
    modelLoaded = false;
    modelFailure = null;
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
onIpc('get-config', () => pushConfigToSettings());
onIpc('get-history', () => pushHistoryToSettings());
onIpc('clear-history', () => clearHistory());
onIpc('list-devices', () => sendToPython('list_devices'));
onIpc('list-models', () => sendToPython('list_models'));
onIpc('retry-model', () => retryModelLoad());
onIpc('hotkey-capture', (_, active) => {
  // A page is recording a new key combo – the global shortcut must not fire
  // (and start a dictation) while the user presses keys to define it.
  if (active) globalShortcut.unregisterAll();
  else if (!isRecording && !isTranscribing) registerHotkeys();
});
onIpc('open-onboarding', () => openOnboarding());
onIpc('open-settings', () => openSettings());
onIpc('close-onboarding', () => {
  // Finished or dismissed – either way, don't show the wizard again.
  markOnboardingDone();
  if (onboardingWin && !onboardingWin.isDestroyed()) onboardingWin.close();
});
// Settings "Send test": the backend posts a fixed test body to the URL and
// token from the form and answers with agent_result (test: true).
onIpc('agent-test', (_, cfg) => {
  const data = { url: String((cfg && cfg.url) || ''), token: String((cfg && cfg.token) || ''), version: app.getVersion() };
  if (pythonProcess && pythonProcess.stdin.writable) sendToPython('agent_test', data);
  else if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.webContents.send('agent-test-result', { test: true, ok: false, status: null, message: '', error: 'backend' });
  }
});
onIpc('ollama-status', () => sendToPython('ollama_status'));
onIpc('ollama-setup', () => sendToPython('ollama_setup'));
onIpc('ollama-update', () => sendToPython('ollama_update'));
onIpc('close-settings', () => {
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.close();
});
onIpc('close-toast', () => {
  if (toastWin && !toastWin.isDestroyed()) toastWin.close();
});

// ── Correction suggestions (dictionary stage 2) ──
// The backend spotted a fix the user typed right after a dictation. The pill
// is clickable but never takes focus – the user keeps typing where they were.
const DICT_MAX = 500;

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
      sandbox: true,
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
    win.webContents.send('set-theme', currentTheme(), currentGas());
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

onIpc('suggest-hover', (_, hovering) => {
  if (!suggestWin || suggestWin.isDestroyed()) return;
  suggestWin.setIgnoreMouseEvents(!hovering, { forward: true });
  // Reading or aiming at the pill keeps it; leaving gives a short grace.
  if (hovering) { if (suggestTimer) clearTimeout(suggestTimer); suggestTimer = null; }
  else armSuggestTimer(4000);
});

onIpc('suggest-answer', (_, learn) => {
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
  historySaver.schedule();  // also persists statsByDate
}

// ── Clipboard History ──
function addToHistory(text) {
  clipboardHistory.unshift({ text, time: Date.now() });
  if (clipboardHistory.length > MAX_HISTORY) clipboardHistory.pop();
  historySaver.schedule();
  updateTray('ready');  // Refresh menu with new history
  pushHistoryToSettings();  // dictionary pane: learn from recent dictations
}

function clearHistory() {
  clipboardHistory = [];
  historySaver.flush();
  updateTray(trayStatus);
  pushHistoryToSettings();
}

function pushHistoryToSettings() {
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('history', clipboardHistory);
}

// ── Toast Notification ──
// Success toasts are a glance: 3 s, click-through. Warnings and errors say
// what happened and what to do, stay longer, hold still while the mouse is on
// them and can carry one button (action: 'restart' | 'settings:<pane>').
const TOAST_DWELL = { success: 3000, warning: 6000, error: 9000 };
let toastTimer = null;

function armToastTimer(ms) {
  if (toastTimer) clearTimeout(toastTimer);
  const win = toastWin;
  toastTimer = setTimeout(() => {
    toastTimer = null;
    if (win && !win.isDestroyed()) win.close();
  }, ms);
}

// Backend errors arrive as English developer text; the user gets the cause
// and the next step instead. Unknown messages keep their text as the detail.
// (A model that won't load: setModelFailed.)
function showBackendError(message) {
  const T = trayT();
  if (message === 'Not recording') return;   // stray stop, nothing for the user to do
  if (message.startsWith('Device listing failed')) showToast(T.devicesFailed, 'error');
  else showToast([T.unknownError, message], 'error');
}

// No model could be loaded. Unlike a 9-second toast this stays: tray,
// settings and setup guide show it, and the hotkey answers with it, until a
// model loads (model_loading / model_ready clear it).
function setModelFailed(data, { toast = true } = {}) {
  modelLoaded = false;
  modelFailure = normalizeFailure(data);
  console.error(`Model failed (${modelFailure.code}): ${modelFailure.message}`);
  if (!isRecording && !isTranscribing) updateTray('ready');   // shown as 'failed'
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('model-failed', modelFailure);
  sendToOnboarding('model-failed', modelFailure);
  if (toast) showModelFailure();
}

function showModelFailure() {
  // The toast's second line holds two lines of text: the next step only.
  // Settings and setup guide show the whole hint.
  const { title, step } = failureText(modelFailure, config.app_language === 'en' ? 'en' : 'de');
  showToast([title, step], 'error', null, failureAction(modelFailure));
}

// "Try again" (toast, tray, settings, setup guide): the backend loads the
// model once more, or a backend that gave up after crashing starts again.
// The answer is model_loading, then model_ready or model_failed.
function retryModelLoad() {
  if (modelLoaded || quitRequested) return;
  if (pythonProcess && pythonProcess.stdin.writable) {
    sendToPython('retry_model');
  } else if (backendGaveUp) {   // during a restart back-off one is coming anyway
    consecutiveFastCrashes = 0;
    crashToastShown = false;
    backendGaveUp = false;
    modelFailure = null;
    updateTray('loading');
    if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('model-loading', {});
    sendToOnboarding('model-loading', {});
    startPython();
  }
}

function showToast(msg, type = 'success', stats = null, action = null) {
  if (toastWin && !toastWin.isDestroyed()) toastWin.close();
  // msg: the dictated text (success), a one-liner, or [title, sub].
  const [title, sub] = Array.isArray(msg) ? msg : [String(msg || ''), ''];
  const T = trayT();
  const actionLabel = !action ? '' : action === 'restart' ? T.actRestart
    : action === 'model:retry' ? T.actRetry
    : action === 'settings:cleanup' ? T.actSetup
    : action === 'settings:system' || action === 'settings:general' ? T.actChange
    : action.startsWith('rescue:') ? T.actRescue : T.actOpen;
  const calm = type === 'success';
  const dwell = TOAST_DWELL[type] || TOAST_DWELL.success;

  // Local reference for all handlers below. The global `toastWin` can be
  // nulled/replaced while this window is still loading (a newer toast closes
  // this one; the async 'closed' handler then nulls the global) – handlers
  // touching the global raced that and crashed with "getBounds of null".
  const win = new BrowserWindow({
    width: calm ? 520 : 580,
    height: calm ? 56 : 96,
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
      sandbox: true,
      backgroundThrottling: false,
    },
  });

  toastWin = win;
  // Errors and warnings forward the mouse so the page can tell when it's
  // hovered; only then does the window take clicks (see 'toast-hover').
  if (calm) win.setIgnoreMouseEvents(true);
  else win.setIgnoreMouseEvents(true, { forward: true });
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
    win.webContents.send('set-theme', currentTheme(), currentGas());
    win.showInactive();
    win.webContents.send('toast-data', {
      text: title, sub, type, stats, action, actionLabel, dwell,
      lang: config.app_language === 'en' ? 'en' : 'de',
    });
  });

  // The page fades itself out after `dwell`; this is the backstop.
  armToastTimer(dwell + 1500);

  win.on('closed', () => {
    if (toastWin !== win) return;
    toastWin = null;
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  });
}

onIpc('toast-hover', (_, hovering) => {
  if (!toastWin || toastWin.isDestroyed()) return;
  toastWin.setIgnoreMouseEvents(!hovering, { forward: true });
  if (hovering) { if (toastTimer) clearTimeout(toastTimer); toastTimer = null; }
  else armToastTimer(4000);
});

onIpc('toast-action', (_, action) => {
  if (toastWin && !toastWin.isDestroyed()) toastWin.close();
  if (action === 'restart') { app.relaunch(); quitApp(); }
  else if (action === 'model:retry') retryModelLoad();
  else if (typeof action === 'string' && action.startsWith('settings:')) openSettings(action.slice(9));
  else if (typeof action === 'string' && action.startsWith('rescue:')) runRescue(action.slice(7));
});

// ── Updates ──
// electron-updater reads latest.yml from the newest GitHub release: 20 s after
// start (on the first run 20 s after the setup guide), then every 6 hours (unless
// switched off in the setup or settings), and whenever the user asks from the
// tray or settings. Nothing downloads before a click. Only the NSIS install
// can replace itself; the portable ZIP and an unpacked build get a link to the
// release page instead.
const RELEASES_URL = 'https://github.com/MJM-DEVS/NoType/releases/latest';
const UPDATE_FIRST_CHECK_MS = 20_000;
const UPDATE_INTERVAL_MS = 6 * 60 * 60 * 1000;
const CAN_SELF_UPDATE = IS_PACKAGED &&
  fs.existsSync(path.join(path.dirname(process.execPath), 'Uninstall NoType.exe'));
// The NSIS installer relaunches the app with --updated after an update.
const JUST_UPDATED = process.argv.includes('--updated');
let autoUpdater = null;          // electron-updater, only in a packaged build
// state: idle | checking | current | available | downloading | ready | error
let update = { state: 'idle', version: null, size: 0, percent: 0, checkedAt: 0 };
let updateManual = false;        // the running check was asked for → report the result
let updateOffered = null;        // version whose pill was shown this session
let updateOfferPending = false;
let updateWin = null;            // "NoType x.y.z ist da" pill
let updateWinTimer = null;
const scheduleFirstUpdateCheck = firstUpdateCheck(() => setTimeout(() => checkForUpdate(false), UPDATE_FIRST_CHECK_MS));

function initUpdater() {
  if (!IS_PACKAGED) return;      // a dev run has no app-update.yml
  try {
    ({ autoUpdater } = require('electron-updater'));
  } catch (e) {
    console.error('Updater unavailable:', e.message);
    return;
  }
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.logger = {
    info: (m) => console.log('[update]', m),
    warn: (m) => console.warn('[update]', m),
    error: (m) => console.error('[update]', m),
    debug: () => {},
  };

  autoUpdater.on('update-available', (info) => {
    const size = (info.files && info.files[0] && info.files[0].size) || 0;
    setUpdate({ state: 'available', version: info.version, size }, true);
    offerUpdate(updateManual);
  });
  autoUpdater.on('update-not-available', () => {
    setUpdate({ state: 'current', version: null }, true);
    if (updateManual) showToast(trayT().updCurrent.replace('{v}', app.getVersion()), 'success');
  });
  autoUpdater.on('download-progress', (p) => {
    const percent = Math.min(99, Math.floor(p.percent || 0));
    if (percent !== update.percent) setUpdate({ percent });
  });
  autoUpdater.on('update-downloaded', () => {
    setUpdate({ state: 'ready', percent: 100 });
    installWhenIdle();
  });
  autoUpdater.on('error', (err) => {
    console.error('[update] failed:', err && err.message);
    if (update.state === 'downloading') {
      setUpdate({ state: 'available', percent: 0 });   // the offer stays, retry works
      showUpdatePill('error');
    } else if (update.state === 'checking') {
      setUpdate({ state: 'error' });
      if (updateManual) showToast(trayT().updFailed, 'error');
    }
  });

  if (CAN_SELF_UPDATE) setTimeout(clearPendingUpdate, UPDATE_FIRST_CHECK_MS / 2);
  scheduleFirstUpdateCheck(config);   // on the first run: once the setup is done
  setInterval(() => checkForUpdate(false), UPDATE_INTERVAL_MS);
}

// electron-updater keeps the installer it ran (about 1 GB) in
// notype-updater\pending until the next download. By now that installer has
// exited, so the copy can go; a later update downloads only what changed.
function clearPendingUpdate() {
  if (['downloading', 'ready'].includes(update.state)) return;
  const local = process.env.LOCALAPPDATA || path.join(app.getPath('home'), 'AppData', 'Local');
  const dir = path.join(local, 'notype-updater', 'pending');
  fs.rm(dir, { recursive: true, force: true }, (err) => {
    if (err) console.warn('[update] could not clear', dir, err.message);
  });
}

function setUpdate(patch, checked = false) {
  update = { ...update, ...patch };
  if (checked) update.checkedAt = Date.now();
  updateTray(trayStatus);
  pushUpdateStatus();
}

function pushUpdateStatus() {
  if (!settingsWin || settingsWin.isDestroyed()) return;
  settingsWin.webContents.send('update-status', {
    supported: !!autoUpdater,
    canInstall: CAN_SELF_UPDATE,
    current: app.getVersion(),
    ...update,
  });
}

function checkForUpdate(manual) {
  if (!autoUpdater) return;
  if (!manual && (config.update_check === false || !config.first_run_complete)) return;
  if (['checking', 'downloading', 'ready'].includes(update.state)) return;
  if (manual && update.state === 'available') { offerUpdate(true); return; }
  updateManual = manual;
  setUpdate({ state: 'checking' });
  autoUpdater.checkForUpdates().catch(() => {});   // failures arrive as 'error'
}

function updateMenuItem(T) {
  switch (update.state) {
    case 'checking': return { label: T.updChecking, enabled: false };
    case 'available': return {
      label: (CAN_SELF_UPDATE ? T.updInstall : T.updDownload).replace('{v}', update.version),
      click: () => startUpdate(),
    };
    case 'downloading': return { label: T.updLoading.replace('{p}', update.percent), enabled: false };
    case 'ready': return { label: T.updInstalling, enabled: false };
    default: return { label: T.updCheck, click: () => checkForUpdate(true) };
  }
}

// The user said yes: download (installed app) or open the release page.
function startUpdate() {
  if (!autoUpdater || update.state !== 'available') return;
  if (!CAN_SELF_UPDATE) {
    shell.openExternal(RELEASES_URL);
    closeUpdatePill();
    return;
  }
  setUpdate({ state: 'downloading', percent: 0 });
  autoUpdater.downloadUpdate().catch(() => {});    // failures arrive as 'error'
}

const updateBusy = () => isRecording || isTranscribing;

function offerUpdate(force = false) {
  if (update.state !== 'available' || updateOfferPending) return;
  if (!force && updateOffered === update.version) return;
  // Not over a running dictation or a correction suggestion.
  if (updateBusy() || (suggestWin && !suggestWin.isDestroyed())) {
    updateOfferPending = true;
    setTimeout(() => { updateOfferPending = false; offerUpdate(force); }, 5000);
    return;
  }
  updateOffered = update.version;
  showUpdatePill('offer');
}

// Downloaded. Never pull the app away mid-dictation: wait for the hotkey to
// be idle, say what happens, then install.
function installWhenIdle() {
  if (updateBusy()) { setTimeout(installWhenIdle, 3000); return; }
  showUpdatePill('installing');
  setTimeout(() => {
    if (updateBusy()) { closeUpdatePill(); setTimeout(installWhenIdle, 3000); return; }
    installUpdate();
  }, 2500);
}

function installUpdate() {
  // Like quitApp(), but the backend has to be gone before the installer
  // starts: NSIS only closes NoType.exe and would find backend.exe still
  // holding its files.
  quitRequested = true;
  historySaver.flush();
  globalShortcut.unregisterAll();
  let started = false;
  const run = () => {
    if (started) return;
    started = true;
    for (const w of [overlayWin, settingsWin, onboardingWin, suggestWin, toastWin, updateWin]) {
      if (w && !w.isDestroyed()) try { w.destroy(); } catch (_) {}
    }
    if (tray) try { tray.destroy(); } catch (_) {}
    autoUpdater.quitAndInstall(true, true);   // silent installer, NoType starts again afterwards
  };
  const proc = pythonProcess;
  if (!proc) { run(); return; }
  proc.once('close', run);
  sendToPython('quit');
  setTimeout(() => { try { proc.kill(); } catch (_) {} }, 4000);
  setTimeout(run, 6000);
}

function closeUpdatePill() {
  if (updateWinTimer) { clearTimeout(updateWinTimer); updateWinTimer = null; }
  if (updateWin && !updateWin.isDestroyed()) updateWin.close();
  updateWin = null;
}

function armUpdatePillTimer(ms) {
  if (updateWinTimer) clearTimeout(updateWinTimer);
  updateWinTimer = setTimeout(closeUpdatePill, ms);
}

// kind: offer | error | installing (same pill as the correction suggestion)
function showUpdatePill(kind) {
  closeUpdatePill();
  const win = new BrowserWindow({
    width: 620,
    height: 96,          // room for the pill's soft shadow
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
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  updateWin = win;
  win.setIgnoreMouseEvents(true, { forward: true });
  win.loadFile(path.join(__dirname, 'update.html'));
  win.once('ready-to-show', () => {
    if (win.isDestroyed()) return;
    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    const { x, y, width, height } = display.workArea;
    const b = win.getBounds();
    win.setPosition(x + Math.round((width - b.width) / 2), y + height - b.height - 32);
    win.webContents.send('set-theme', currentTheme(), currentGas());
    win.webContents.send('update-data', {
      kind, version: update.version, size: update.size, canInstall: CAN_SELF_UPDATE,
      lang: config.app_language === 'en' ? 'en' : 'de',
    });
    win.showInactive();
  });
  armUpdatePillTimer(kind === 'installing' ? 10_000 : 15_000);
  win.on('closed', () => { if (updateWin === win) closeUpdatePill(); });
}

onIpc('update-hover', (_, hovering) => {
  if (!updateWin || updateWin.isDestroyed()) return;
  updateWin.setIgnoreMouseEvents(!hovering, { forward: true });
  if (hovering) { if (updateWinTimer) clearTimeout(updateWinTimer); updateWinTimer = null; }
  else armUpdatePillTimer(5000);
});

onIpc('update-action', (_, action) => {
  if (action === 'check') checkForUpdate(true);
  else if (action === 'status') pushUpdateStatus();
  else if (action === 'dismiss') closeUpdatePill();
  else if (action === 'install') {
    startUpdate();
    // The pill confirms the download, then gets out of the way.
    if (updateWin && !updateWin.isDestroyed()) armUpdatePillTimer(3000);
  }
});

// ── App Lifecycle ──
function quitApp() {
  quitRequested = true;  // suppress backend auto-restart
  historySaver.flush();  // final synchronous write, drops the pending debounced one
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

// ── Navigation & permissions ──
// Every window shows one local page and has no links out. Nothing may
// navigate it elsewhere or open new windows; the update pill's release page
// opens from the main process (shell.openExternal), not from a page.
app.on('web-contents-created', (_, contents) => {
  // DevTools (dev runs) load their own devtools:// pages.
  if (contents.getType() === 'devtools') return;
  contents.on('will-navigate', (e, url) => {
    if (!isSameDocument(contents.getURL(), url)) {
      e.preventDefault();
      console.warn('Blocked navigation away from the app page');
    }
  });
  // Sub-frames: only the settings page's overlay preview (overlay.html).
  contents.on('will-frame-navigate', (e) => {
    if (!e.isMainFrame && !isAppFile(e.url, __dirname)) e.preventDefault();
  });
  contents.setWindowOpenHandler(() => {
    console.warn('Blocked a new window from an app page');
    return { action: 'deny' };
  });
});

function installPermissionHandler() {
  // Permission requests: the microphone (audio only) for the mic tests in
  // settings and setup; every other request and every other page is refused.
  // Synchronous permission checks keep Electron's default.
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
    const pageUrl = (details && details.requestingUrl) || contents.getURL();
    callback(allowPermission(permission, pageUrl, details, __dirname));
  });
}

app.whenReady().then(() => {
  installPermissionHandler();
  initPaths();
  loadConfig();
  syncThemeSource();
  loadHistory();
  // Overlay is created lazily on the first recording (showOverlay) so no
  // transparent always-on-top compositor surface exists until actually needed.
  createTrayWithRetry();
  startPython();
  startBackendWatchdog();
  initUpdater();
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
