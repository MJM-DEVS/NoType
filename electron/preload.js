const { contextBridge, ipcRenderer } = require('electron');

// Every on* subscription returns its unsubscribe function (callers that
// ignore it keep working as before).
function sub(channel, listener) {
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('notype', {
  // Receive messages from main process
  onAmplitude: (callback) => sub('amplitude', (_, val, bands) => callback(val, bands)),
  onRecordingStarted: (callback) => sub('recording-started', (_, opts) => callback(opts || {})),
  onRecordingStopped: (callback) => sub('recording-stopped', () => callback()),
  onRecordingCancelled: (callback) => sub('recording-cancelled', () => callback()),
  onRecordingPaused: (callback) => sub('recording-paused', () => callback()),
  onRecordingResumed: (callback) => sub('recording-resumed', () => callback()),
  onTranscribing: (callback) => sub('transcribing', () => callback()),
  onTranscriptionDone: (callback) => sub('transcription-done', (_, data) => callback(data)),
  onSetTheme: (callback) => sub('set-theme', (_, theme, gas) => callback(theme, gas)),
  onSetStyle: (callback) => sub('set-style', (_, style, opts) => callback(style, opts || {})),
  onStatus: (callback) => sub('status', (_, msg) => callback(msg)),
  onModelReady: (callback) => sub('model-ready', (_, data) => callback(data)),
  onConfig: (callback) => sub('config', (_, data) => callback(data)),
  onDevices: (callback) => sub('devices', (_, data) => callback(data)),
  onToastData: (callback) => sub('toast-data', (_, data) => callback(data)),
  // Settings: jump to a section (from a toast's "open" button).
  onShowPane: (callback) => sub('show-pane', (_, pane) => callback(String(pane || ''))),
  onOllamaStatus: (callback) => sub('ollama-status', (_, data) => callback(data)),
  onOllamaProgress: (callback) => sub('ollama-progress', (_, data) => callback(data)),
  onPreviewText: (callback) => sub('preview-text', (_, text) => callback(text)),
  onPreviewEnabled: (callback) => sub('preview-enabled', (_, enabled) => callback(enabled)),
  onAppLang: (callback) => sub('app-lang', (_, lang) => callback(lang)),
  onModels: (callback) => sub('models', (_, data) => callback(data)),
  onBusy: (callback) => sub('busy', () => callback()),
  onOverlayLeaving: (callback) => sub('overlay-leaving', () => callback()),
  onAttachment: (callback) => sub('attachment', (_, data) => callback(data || {})),
  onHistory: (callback) => sub('history', (_, list) => callback(list)),
  onSuggestData: (callback) => sub('suggest-data', (_, data) => callback(data)),
  onDictionary: (callback) => sub('dictionary', (_, entries) => callback(entries)),
  onUpdateData: (callback) => sub('update-data', (_, data) => callback(data)),
  onUpdateStatus: (callback) => sub('update-status', (_, status) => callback(status)),
  // Agent hotkey: the text is on its way, the URL's answer, the settings test.
  onAgentSending: (callback) => sub('agent-sending', (_, data) => callback(data || {})),
  onAgentResult: (callback) => sub('agent-result', (_, data) => callback(data || {})),
  onAgentTestResult: (callback) => sub('agent-test-result', (_, data) => callback(data || {})),

  // Send messages to main process
  saveConfig: (config) => ipcRenderer.send('save-config', config),
  getConfig: () => ipcRenderer.send('get-config'),
  getHistory: () => ipcRenderer.send('get-history'),
  clearHistory: () => ipcRenderer.send('clear-history'),
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
  ollamaStatus: () => ipcRenderer.send('ollama-status'),
  ollamaSetup: () => ipcRenderer.send('ollama-setup'),
  ollamaUpdate: () => ipcRenderer.send('ollama-update'),
  // Agent hotkey: POST a test body to { url, token } from the settings form.
  agentTest: (cfg) => ipcRenderer.send('agent-test', {
    url: String((cfg && cfg.url) || ''), token: String((cfg && cfg.token) || ''),
  }),
});
