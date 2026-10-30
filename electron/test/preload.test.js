// preload.js against a stubbed electron module: every on* subscription
// returns a working unsubscribe, and the exposed API stays the same.
const test = require('node:test');
const assert = require('node:assert');
const Module = require('module');
const { EventEmitter } = require('events');

function loadPreload() {
  const ipcRenderer = new EventEmitter();
  ipcRenderer.sent = [];
  ipcRenderer.send = (...args) => ipcRenderer.sent.push(args);
  let api = null;
  const electron = { ipcRenderer, contextBridge: { exposeInMainWorld: (name, obj) => { api = obj; } } };
  const origLoad = Module._load;
  Module._load = function (request, ...rest) {
    return request === 'electron' ? electron : origLoad.call(this, request, ...rest);
  };
  try {
    delete require.cache[require.resolve('../preload')];
    require('../preload');
  } finally {
    Module._load = origLoad;
  }
  return { api, ipcRenderer };
}

test('on* returns an unsubscribe function', () => {
  const { api, ipcRenderer } = loadPreload();
  const got = [];
  const off = api.onAmplitude((v, bands) => got.push([v, bands]));
  assert.strictEqual(typeof off, 'function');
  ipcRenderer.emit('amplitude', {}, 0.5, [1, 2]);
  off();
  ipcRenderer.emit('amplitude', {}, 0.9, null);
  assert.deepStrictEqual(got, [[0.5, [1, 2]]]);
  assert.strictEqual(ipcRenderer.listenerCount('amplitude'), 0);
});

test('every on* subscribes and unsubscribes cleanly', () => {
  const { api, ipcRenderer } = loadPreload();
  const subs = Object.keys(api).filter((k) => /^on[A-Z]/.test(k));
  assert.ok(subs.length >= 30);
  const offs = subs.map((k) => api[k](() => {}));
  assert.ok(offs.every((f) => typeof f === 'function'));
  offs.forEach((f) => f());
  assert.strictEqual(ipcRenderer.eventNames().length, 0);
});

test('argument mapping is unchanged', () => {
  const { api, ipcRenderer } = loadPreload();
  let opts, pane;
  api.onRecordingStarted((o) => { opts = o; });
  api.onShowPane((p) => { pane = p; });
  ipcRenderer.emit('recording-started', {});
  ipcRenderer.emit('show-pane', {}, null);
  assert.deepStrictEqual(opts, {});
  assert.strictEqual(pane, '');
});

test('the unused registerHotkey is gone, senders still send', () => {
  const { api, ipcRenderer } = loadPreload();
  assert.strictEqual(api.registerHotkey, undefined);
  api.setHotkeyCapture(1);
  api.agentTest({ url: 'https://x', token: null });
  assert.deepStrictEqual(ipcRenderer.sent, [
    ['hotkey-capture', true],
    ['agent-test', { url: 'https://x', token: '' }],
  ]);
});

test('clearHistory sends no arguments', () => {
  const { api, ipcRenderer } = loadPreload();
  api.clearHistory('ignored');
  assert.deepStrictEqual(ipcRenderer.sent, [['clear-history']]);
});
