const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('notype', {
  // Receive messages from main process
  onAmplitude: (callback) => ipcRenderer.on('amplitude', (_, val) => callback(val)),
  onRecordingStarted: (callback) => ipcRenderer.on('recording-started', () => callback()),
  onRecordingStopped: (callback) => ipcRenderer.on('recording-stopped', () => callback()),
  onRecordingPaused: (callback) => ipcRenderer.on('recording-paused', () => callback()),
  onRecordingResumed: (callback) => ipcRenderer.on('recording-resumed', () => callback()),
  onTranscribing: (callback) => ipcRenderer.on('transcribing', () => callback()),
  onTranscriptionDone: (callback) => ipcRenderer.on('transcription-done', (_, data) => callback(data)),
  onSetTheme: (callback) => ipcRenderer.on('set-theme', (_, theme) => callback(theme)),
  onSetStyle: (callback) => ipcRenderer.on('set-style', (_, style) => callback(style)),
  onStatus: (callback) => ipcRenderer.on('status', (_, msg) => callback(msg)),
  onModelReady: (callback) => ipcRenderer.on('model-ready', (_, data) => callback(data)),
  onConfig: (callback) => ipcRenderer.on('config', (_, data) => callback(data)),
  onDevices: (callback) => ipcRenderer.on('devices', (_, data) => callback(data)),
  onToastData: (callback) => ipcRenderer.on('toast-data', (_, data) => callback(data)),
  onOllamaStatus: (callback) => ipcRenderer.on('ollama-status', (_, data) => callback(data)),
  onOllamaProgress: (callback) => ipcRenderer.on('ollama-progress', (_, data) => callback(data)),
  onPreviewText: (callback) => ipcRenderer.on('preview-text', (_, text) => callback(text)),
  onPreviewEnabled: (callback) => ipcRenderer.on('preview-enabled', (_, enabled) => callback(enabled)),

  // Send messages to main process
  saveConfig: (config) => ipcRenderer.send('save-config', config),
  getConfig: () => ipcRenderer.send('get-config'),
  listDevices: () => ipcRenderer.send('list-devices'),
  closeSettings: () => ipcRenderer.send('close-settings'),
  closeToast: () => ipcRenderer.send('close-toast'),
  registerHotkey: (key) => ipcRenderer.send('register-hotkey', key),
  ollamaStatus: () => ipcRenderer.send('ollama-status'),
  ollamaSetup: () => ipcRenderer.send('ollama-setup'),
});
