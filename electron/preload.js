const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('notype', {
  // Receive messages from main process
  onAmplitude: (callback) => ipcRenderer.on('amplitude', (_, val, bands) => callback(val, bands)),
  onRecordingStarted: (callback) => ipcRenderer.on('recording-started', (_, opts) => callback(opts || {})),
  onRecordingStopped: (callback) => ipcRenderer.on('recording-stopped', () => callback()),
  onRecordingCancelled: (callback) => ipcRenderer.on('recording-cancelled', () => callback()),
  onRecordingPaused: (callback) => ipcRenderer.on('recording-paused', () => callback()),
  onRecordingResumed: (callback) => ipcRenderer.on('recording-resumed', () => callback()),
  onTranscribing: (callback) => ipcRenderer.on('transcribing', () => callback()),
  onTranscriptionDone: (callback) => ipcRenderer.on('transcription-done', (_, data) => callback(data)),
  onSetTheme: (callback) => ipcRenderer.on('set-theme', (_, theme, gas) => callback(theme, gas)),
  onSetStyle: (callback) => ipcRenderer.on('set-style', (_, style, opts) => callback(style, opts || {})),
  onStatus: (callback) => ipcRenderer.on('status', (_, msg) => callback(msg)),
  onModelReady: (callback) => ipcRenderer.on('model-ready', (_, data) => callback(data)),
  onConfig: (callback) => ipcRenderer.on('config', (_, data) => callback(data)),
  onDevices: (callback) => ipcRenderer.on('devices', (_, data) => callback(data)),
  onToastData: (callback) => ipcRenderer.on('toast-data', (_, data) => callback(data)),
  // Settings: jump to a section (from a toast's "open" button).
  onShowPane: (callback) => ipcRenderer.on('show-pane', (_, pane) => callback(String(pane || ''))),
  onOllamaStatus: (callback) => ipcRenderer.on('ollama-status', (_, data) => callback(data)),
  onOllamaProgress: (callback) => ipcRenderer.on('ollama-progress', (_, data) => callback(data)),
  onPreviewText: (callback) => ipcRenderer.on('preview-text', (_, text) => callback(text)),
  onPreviewEnabled: (callback) => ipcRenderer.on('preview-enabled', (_, enabled) => callback(enabled)),
  onAppLang: (callback) => ipcRenderer.on('app-lang', (_, lang) => callback(lang)),
  onModels: (callback) => ipcRenderer.on('models', (_, data) => callback(data)),
  onBusy: (callback) => ipcRenderer.on('busy', () => callback()),
  onOverlayLeaving: (callback) => ipcRenderer.on('overlay-leaving', () => callback()),
  onAttachment: (callback) => ipcRenderer.on('attachment', (_, data) => callback(data || {})),
  onHistory: (callback) => ipcRenderer.on('history', (_, list) => callback(list)),
  onSuggestData: (callback) => ipcRenderer.on('suggest-data', (_, data) => callback(data)),
  onDictionary: (callback) => ipcRenderer.on('dictionary', (_, entries) => callback(entries)),
  onUpdateData: (callback) => ipcRenderer.on('update-data', (_, data) => callback(data)),
  onUpdateStatus: (callback) => ipcRenderer.on('update-status', (_, status) => callback(status)),
  // Agent hotkey: the text is on its way, the URL's answer, the settings test.
  onAgentSending: (callback) => ipcRenderer.on('agent-sending', (_, data) => callback(data || {})),
  onAgentResult: (callback) => ipcRenderer.on('agent-result', (_, data) => callback(data || {})),
  onAgentTestResult: (callback) => ipcRenderer.on('agent-test-result', (_, data) => callback(data || {})),

  // Send messages to main process
  saveConfig: (config) => ipcRenderer.send('save-config', config),
  getConfig: () => ipcRenderer.send('get-config'),
  getHistory: () => ipcRenderer.send('get-history'),
  listDevices: () => ipcRenderer.send('list-devices'),
  listModels: () => ipcRenderer.send('list-models'),
  // While a page captures a new hotkey, the global shortcut must not fire.
  setHotkeyCapture: (active) => ipcRenderer.send('hotkey-capture', !!active),
  openOnboarding: () => ipcRenderer.send('open-onboarding'),
  closeOnboarding: () => ipcRenderer.send('close-onboarding'),
  openSettings: () => ipcRenderer.send('open-settings'),
  closeSettings: () => ipcRenderer.send('close-settings'),
  closeToast: () => ipcRenderer.send('close-toast'),
  // Error toasts: hovered (holds it open, takes clicks) and their one button.
  toastHover: (hovering) => ipcRenderer.send('toast-hover', !!hovering),
  toastAction: (action) => ipcRenderer.send('toast-action', String(action)),
  // Correction suggestion: learn the shown pairs, or dismiss (false).
  suggestAnswer: (learn) => ipcRenderer.send('suggest-answer', !!learn),
  suggestHover: (hovering) => ipcRenderer.send('suggest-hover', !!hovering),
  // Updates: 'check' | 'install' | 'dismiss' | 'status'
  updateAction: (action) => ipcRenderer.send('update-action', String(action)),
  updateHover: (hovering) => ipcRenderer.send('update-hover', !!hovering),
  registerHotkey: (key) => ipcRenderer.send('register-hotkey', key),
  ollamaStatus: () => ipcRenderer.send('ollama-status'),
  ollamaSetup: () => ipcRenderer.send('ollama-setup'),
  ollamaUpdate: () => ipcRenderer.send('ollama-update'),
  // Agent hotkey: POST a test body to { url, token } from the settings form.
  agentTest: (cfg) => ipcRenderer.send('agent-test', {
    url: String((cfg && cfg.url) || ''), token: String((cfg && cfg.token) || ''),
  }),
});
