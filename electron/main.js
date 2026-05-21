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

// ── State ──
let tray = null;
let overlayWin = null;
let settingsWin = null;
let toastWin = null;
let pythonProcess = null;
let pythonStartCount = 0;     // how many times we've spawned the backend
let lastBackendDeath = 0;     // ms timestamp of the last unexpected exit
let quitRequested = false;    // set by quitApp() to suppress restart
let isRecording = false;
let isPaused = false;
let isTranscribing = false;
let modelLoaded = false;
let modelInfo = null;
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
  for (const w of [settingsWin, overlayWin, toastWin]) {
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
    overlay_style: 'wave',
    first_run_complete: false,
    ...config,
  };
  return config;
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

    // If we asked it to quit, don't relaunch.
    if (quitRequested) return;

    // If any recording was in flight, reset the state machine so the hotkey
    // doesn't end up locked.
    if (isRecording || isTranscribing) {
      finishTranscription();
      showToast('Backend abgestürzt – starte neu', 'error');
    } else {
      showToast('Backend ist abgestürzt – starte neu', 'error');
    }

    // Throttle restart loop: if we crashed twice in <10 s, back off to avoid
    // a respawn storm (e.g. a corrupt settings.json that crashes the model
    // load every time).
    const now = Date.now();
    const tooSoon = (now - lastBackendDeath) < 10_000;
    lastBackendDeath = now;
    const delay = tooSoon ? 8_000 : 2_000;
    console.log(`Restarting backend in ${delay} ms (attempt #${pythonStartCount + 1})`);
    setTimeout(() => { if (!quitRequested) startPython(); }, delay);
  });

  pythonProcess.on('error', (err) => {
    console.error('Backend spawn error:', err);
  });

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

      case 'amplitude':
        if (overlayWin && !overlayWin.isDestroyed()) {
          overlayWin.webContents.send('amplitude', data.value);
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


      case 'transcription_done':
        finishTranscription();
        if (data && data.text && !data.empty) {
          addToHistory(data.text);
          trackStats(data.text);
          showToast(data.text, 'success', {
            beam: data.beam,
            compute_type: data.compute_type,
            duration: data.duration,
          });
        } else if (data && data.reason === 'too_short') {
          showToast('Aufnahme zu kurz – verworfen', 'warning');
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
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('model-ready', data);
        }
        break;

      case 'config':
        // If the backend's hotkey/mode differ from what we registered at boot, re-register.
        {
          const prevHotkey = config.hotkey, prevMode = config.mode;
          config = data;
          if (data.hotkey !== prevHotkey || data.mode !== prevMode) {
            registerHotkeys();
          }
        }
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('config', data);
        }
        break;

      case 'config_saved':
        config = data;
        registerHotkeys();
        break;

      case 'status':
        console.log('Status:', data.message);
        break;

      case 'devices':
        if (settingsWin && !settingsWin.isDestroyed()) {
          settingsWin.webContents.send('devices', data.devices);
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
const OVERLAY_STYLES = {
  wave_classic: { w: 600, h: 48,  anchor: 'bottom', offset: 60 },
  wave:         { w: 600, h: 72,  anchor: 'bottom', offset: 60 },
  aurora:       { w: 320, h: 320, anchor: 'lower',  offset: 0  },
  particles:    { w: 640, h: 96,  anchor: 'bottom', offset: 56 },
  pulse:        { w: 180, h: 180, anchor: 'lower',  offset: 0  },
  ribbon:       { w: 600, h: 80,  anchor: 'bottom', offset: 60 },
  spectrum:     { w: 320, h: 100, anchor: 'bottom', offset: 60 },
};
const DEFAULT_OVERLAY_STYLE = 'wave';

function getStyleConfig(name) {
  return OVERLAY_STYLES[name] || OVERLAY_STYLES[DEFAULT_OVERLAY_STYLE];
}

// Apply the right size + anchor for the current overlay style.
function applyOverlayBounds() {
  if (!overlayWin || overlayWin.isDestroyed()) return;
  const styleName = OVERLAY_STYLES[config.overlay_style] ? config.overlay_style : DEFAULT_OVERLAY_STYLE;
  const s = getStyleConfig(styleName);

  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);
  const { x, y, width, height } = display.workArea;

  overlayWin.setBounds({ width: s.w, height: s.h, x: 0, y: 0 });

  const posX = x + Math.round((width - s.w) / 2);
  let posY;
  switch (s.anchor) {
    case 'top':    posY = y + s.offset; break;
    case 'lower':  posY = y + Math.round(height * 0.62); break;
    case 'bottom':
    default:       posY = y + height - s.h - s.offset; break;
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

  overlayWin.setIgnoreMouseEvents(true);
  overlayWin.loadFile(path.join(__dirname, 'overlay.html'));

  overlayWin.on('closed', () => {
    overlayWin = null;
  });
}

function showOverlay() {
  if (!overlayWin || overlayWin.isDestroyed()) createOverlay();
  const styleName = OVERLAY_STYLES[config.overlay_style] ? config.overlay_style : DEFAULT_OVERLAY_STYLE;
  applyOverlayBounds();
  overlayWin.showInactive();
  overlayWin.webContents.send('set-style', styleName);
  overlayWin.webContents.send('set-theme', currentTheme());
  overlayWin.webContents.send('recording-started');
}

function hideOverlay() {
  if (overlayWin && !overlayWin.isDestroyed()) {
    overlayWin.hide();
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

function registerHotkeys() {
  globalShortcut.unregisterAll();
  if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }

  const hotkey = configHotkeyToElectron(config.hotkey);
  const mode = config.mode || 'press_to_speak';
  console.log(`Registering hotkey: ${hotkey} (mode: ${mode})`);

  try {
    const success = globalShortcut.register(hotkey, () => {
      if (mode === 'hold_to_speak') {
        // Hold-to-speak: globalShortcut fires repeatedly while held.
        // Start recording on first fire, reset release-detection timer each fire.
        if (!isRecording && !isTranscribing) {
          startRecording();
        }
        // Reset the "release" timer – if no callback in 350ms, keys were released
        if (holdTimer) clearTimeout(holdTimer);
        holdTimer = setTimeout(() => {
          holdTimer = null;
          if (isRecording) stopRecording();
        }, 350);
      } else {
        // Press-to-speak: toggle
        if (!isRecording) {
          startRecording();
        } else {
          stopRecording();
        }
      }
    });

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
}

function startRecording() {
  if (isTranscribing) return;  // Don't start while transcribing
  if (isRecording) return;     // Already recording – ignore re-trigger
  if (!modelLoaded) {
    showToast('Modell lädt noch – bitte kurz warten', 'warning');
    return;
  }
  isRecording = true;
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
  // Live-preview happens DURING speech. After release we hide the overlay –
  // the final transcript surfaces through the toast notification.
  if (overlayWin && !overlayWin.isDestroyed()) {
    overlayWin.webContents.send('recording-stopped');
    setTimeout(() => hideOverlay(), 250);
  }
  // Unregister hotkeys during paste to prevent Ctrl+V from triggering
  globalShortcut.unregisterAll();
  sendToPython('stop_recording');
  updateTray('processing');

  // Safety net: if the backend NEVER replies (process crashed, IPC stuck),
  // make sure we unlock after 30 s instead of leaving the user dead.
  setTimeout(() => {
    if (isTranscribing) {
      console.error('Transcription timeout – forcing state reset');
      finishTranscription();
      showToast('Transkription hängt – Status zurückgesetzt', 'error');
    }
  }, 30_000);
}

// Single place to reset post-recording state. Called from `transcription_done`,
// from `error`, and from the timeout watchdog above.
function finishTranscription() {
  isRecording = false;
  isTranscribing = false;
  isPaused = false;
  hideOverlay();
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
  const labels = {
    ready: '✓ Bereit',
    recording: '● Aufnahme...',
    processing: '⟳ Transkribiere...',
    loading: '⏳ Modell lädt...',
    paused: '⏸ Pausiert',
  };

  // Override status if model not loaded
  if (!modelLoaded && status === 'ready') status = 'loading';

  try {
    tray.setImage(createTrayImage(colors[status] || '#00d4aa', status === 'recording'));
    const hotkeyLabel = config.hotkey || 'Ctrl+Shift+Space';
    tray.setToolTip(`NoType – ${labels[status] || 'Bereit'} (${hotkeyLabel})`);
    tray.setContextMenu(buildTrayMenu(status));
  } catch (e) {
    console.error('Tray update error:', e);
  }
}

function buildTrayMenu(status = 'ready') {
  const labels = {
    ready: '✓ Bereit',
    recording: '● Aufnahme...',
    processing: '⟳ Transkribiere...',
    loading: '⏳ Modell lädt...',
    paused: '⏸ Pausiert',
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
    { label: `NoType – ${labels[status] || 'Bereit'}`, enabled: false },
    { label: `Hotkey: ${hotkeyLabel}`, enabled: false },
    {
      label: `Heute: ${todayStats.count} · ~${todayStats.words} Wörter`,
      submenu: [
        { label: `Heute:        ${todayStats.count} Transkriptionen, ~${todayStats.words} Wörter`, enabled: false },
        { label: `Letzte 7 Tage:  ${w7.count} Transkriptionen, ~${w7.words} Wörter`, enabled: false },
        { label: `Letzte 30 Tage: ${w30.count} Transkriptionen, ~${w30.words} Wörter`, enabled: false },
      ],
    },
    { type: 'separator' },
  ];

  // Clipboard history
  if (clipboardHistory.length > 0) {
    items.push({ label: 'Letzte Transkriptionen', enabled: false });
    clipboardHistory.forEach((entry, i) => {
      const preview = entry.text.length > 45 ? entry.text.slice(0, 45) + '…' : entry.text;
      const time = new Date(entry.time).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
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
    { label: '⚙  Einstellungen', click: () => openSettings() },
    { type: 'separator' },
    { label: '✕  Beenden', click: () => quitApp() },
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
    width: 560,
    height: 780,
    minWidth: 480,
    minHeight: 600,
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
    sendToPython('get_config');
    // Sync model status
    if (modelLoaded && modelInfo) {
      settingsWin.webContents.send('model-ready', modelInfo);
    }
  });

  settingsWin.on('closed', () => { settingsWin = null; });
}

// ── IPC from renderers ──
ipcMain.on('save-config', (_, newConfig) => sendToPython('save_config', newConfig));
ipcMain.on('get-config', () => sendToPython('get_config'));
ipcMain.on('list-devices', () => sendToPython('list_devices'));
ipcMain.on('close-settings', () => {
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.close();
});
ipcMain.on('close-toast', () => {
  if (toastWin && !toastWin.isDestroyed()) toastWin.close();
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
  saveHistory();  // also persists statsByDate
}

// ── Clipboard History ──
function addToHistory(text) {
  clipboardHistory.unshift({ text, time: Date.now() });
  if (clipboardHistory.length > MAX_HISTORY) clipboardHistory.pop();
  saveHistory();
  updateTray('ready');  // Refresh menu with new history
}

// ── Toast Notification ──
function showToast(text, type = 'success', stats = null) {
  if (toastWin && !toastWin.isDestroyed()) toastWin.close();

  toastWin = new BrowserWindow({
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

  toastWin.setIgnoreMouseEvents(true);
  toastWin.loadFile(path.join(__dirname, 'toast.html'));

  toastWin.once('ready-to-show', () => {
    const cursor = screen.getCursorScreenPoint();
    const display = screen.getDisplayNearestPoint(cursor);
    const { x, y, width, height } = display.workArea;
    const b = toastWin.getBounds();
    toastWin.setPosition(
      x + Math.round((width - b.width) / 2),
      y + height - b.height - 50
    );
    toastWin.webContents.send('set-theme', currentTheme());
    toastWin.showInactive();
    toastWin.webContents.send('toast-data', { text, type, stats });
  });

  // Auto-close after 3 seconds
  setTimeout(() => {
    if (toastWin && !toastWin.isDestroyed()) toastWin.close();
  }, 3000);

  toastWin.on('closed', () => { toastWin = null; });
}

// ── App Lifecycle ──
function quitApp() {
  quitRequested = true;  // suppress backend auto-restart
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
  createOverlay();  // Created hidden, never shows until hotkey
  createTrayWithRetry();
  startPython();
  startBackendWatchdog();
  // Register hotkeys immediately – globalShortcut doesn't depend on the Python backend.
  // The backend may later push an updated config that re-registers via `config_saved`.
  registerHotkeys();

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
