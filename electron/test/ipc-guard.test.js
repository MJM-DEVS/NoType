const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { CHANNEL_PAGES, CONFIG_TYPES, senderPage, allowSender, filterConfig } = require('../lib/ipc-guard');

const APP = path.resolve(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(APP, f), 'utf-8');

// A fake BrowserWindow / IPC event: only what senderPage() reads.
let nextId = 1;
const fakeWin = () => {
  const w = { destroyed: false, webContents: { id: nextId++ }, isDestroyed: () => w.destroyed };
  return w;
};
const fromWin = (w, frame = { parent: null }) => ({ sender: { id: w.webContents.id }, senderFrame: frame });

test('senders: the page is the window that sent, top frame only', () => {
  const settings = fakeWin(), toast = fakeWin(), stray = fakeWin();
  const windows = { 'settings.html': settings, 'toast.html': toast, 'update.html': null };
  assert.strictEqual(senderPage(fromWin(settings), windows), 'settings.html');
  assert.strictEqual(senderPage(fromWin(toast), windows), 'toast.html');
  assert.strictEqual(senderPage(fromWin(stray), windows), null);
  // A sub-frame (the overlay preview in settings) is not the page itself.
  assert.strictEqual(senderPage(fromWin(settings, { parent: {} }), windows), null);
  // Frame already gone, or a window that is closing.
  assert.strictEqual(senderPage(fromWin(settings, null), windows), null);
  toast.destroyed = true;
  assert.strictEqual(senderPage(fromWin(toast), windows), null);
  // A getter that throws on a torn-down frame drops the message, no crash.
  const broken = { sender: { id: settings.webContents.id }, get senderFrame() { throw new Error('gone'); } };
  assert.strictEqual(senderPage(broken, windows), null);
  assert.strictEqual(senderPage({}, windows), null);
});

test('senders: each channel only from its pages', () => {
  assert.ok(allowSender('save-config', 'settings.html'));
  assert.ok(allowSender('save-config', 'onboarding.html'));
  assert.ok(allowSender('update-action', 'update.html'));
  assert.ok(allowSender('update-action', 'settings.html'));
  assert.ok(allowSender('toast-action', 'toast.html'));
  for (const p of ['toast.html', 'overlay.html', 'update.html', 'suggest.html']) {
    assert.ok(!allowSender('save-config', p), p);
    assert.ok(!allowSender('agent-test', p), p);
  }
  assert.ok(!allowSender('save-config', null));
  assert.ok(!allowSender('unknown-channel', 'settings.html'));
  assert.ok(!allowSender('toString', 'settings.html'));
  assert.ok(!allowSender('__proto__', 'settings.html'));
});

// main.js maps each page to the variable of the window that loads it.
test('window map in main.js matches the windows that load each page', () => {
  const main = read('main.js');
  const map = main.match(/const appWindows = \(\) => \(\{([\s\S]*?)\}\);/)[1];
  const pairs = [...map.matchAll(/'([\w-]+\.html)':\s*(\w+)/g)].map((m) => [m[1], m[2]]);
  const pages = fs.readdirSync(APP).filter((f) => f.endsWith('.html')).sort();
  assert.deepStrictEqual(pairs.map(([p]) => p).sort(), pages);
  for (const [p, v] of pairs) {
    // The first page loaded after `v = new BrowserWindow(` or `v = win;`.
    const m = main.match(new RegExp(`\\b${v} = (?:new BrowserWindow\\(|win;)[\\s\\S]*?loadFile\\(path\\.join\\(__dirname, '([\\w-]+\\.html)'\\)`));
    assert.ok(m, `${v} is assigned a window`);
    assert.strictEqual(m[1], p, `${v} loads ${p}`);
  }
});

// What the pages send must reach main.js: every preload channel is in the
// table, every page that calls it is allowed, and main.js listens to it.
test('channel table matches preload.js, the pages and main.js', () => {
  const preload = read('preload.js');
  const apiToChannel = {};
  for (const m of preload.matchAll(/(\w+):\s*\([^)]*\)\s*=>\s*ipcRenderer\.send\('([\w-]+)'/g)) apiToChannel[m[1]] = m[2];
  const sent = new Set(Object.values(apiToChannel));
  assert.ok(sent.size >= 20);
  assert.deepStrictEqual([...sent].sort(), Object.keys(CHANNEL_PAGES).sort());

  const main = read('main.js');
  const handled = [...main.matchAll(/onIpc\('([\w-]+)'/g)].map((m) => m[1]);
  assert.deepStrictEqual([...new Set(handled)].sort(), Object.keys(CHANNEL_PAGES).sort());
  assert.ok(!/ipcMain\.(on|handle|once)\('/.test(main), 'register through onIpc()');

  const pages = fs.readdirSync(APP).filter((f) => f.endsWith('.html'));
  for (const p of pages) {
    const html = read(p);
    for (const [api, channel] of Object.entries(apiToChannel)) {
      if (new RegExp(`\\.${api}\\(`).test(html)) {
        assert.ok(CHANNEL_PAGES[channel].includes(p), `${p} calls ${api} (${channel})`);
      }
    }
  }
});

test('config: what settings.html saves passes unchanged', () => {
  const save = {
    app_language: 'en', hotkey: 'ctrl+shift+space', mode: 'hold_to_speak',
    command_enabled: true, learn_corrections: true, save_history: false,
    command_hotkey: 'ctrl+alt+space', ai_context: true,
    agent_enabled: true, agent_hotkey: 'ctrl+shift+alt+space',
    agent_url: 'http://localhost:5678/hook', agent_token: 'abc', agent_send_context: false,
    languages: [{ code: 'de', share: 80 }, { code: 'en' }], language: 'de',
    auto_language_detect: true, spoken_punctuation: true, model_size: 'small',
    overlay_theme: 'system', overlay_style: 'amoled', overlay_accent: 'mint',
    overlay_position: 'bottom', overlay_sounds: true, gpu_safe_mode: false,
    initial_prompt: 'NoType, Kubernetes', dictionary: [{ from: 'kuber netis', to: 'Kubernetes' }],
    cleanup_mode: 'fast', llm_model: 'qwen3:4b-instruct', live_preview_enabled: true,
    success_toast: false, autostart: false, update_check: true, insert_method: 'auto',
    mic_device_name: null, mic_device_index: null, mute_while_dictating: false,
    beam_size: 'auto', compute_type: 'float16', vad_sensitivity: 300, first_run_complete: true,
  };
  const r = filterConfig(save);
  assert.deepStrictEqual(r.config, save);
  assert.deepStrictEqual(r.rejected, []);
  // The keys of the form above are the ones the page really sends.
  const block = read('settings.html').match(/const newConfig = \{([\s\S]*?)\n {4}\};/)[1];
  const keys = [...block.matchAll(/^\s+(\w+):/gm)].map((m) => m[1]);
  assert.deepStrictEqual(keys.sort(), Object.keys(save).sort());
});

test('config: the onboarding steps pass', () => {
  const samples = [
    { app_language: 'de' }, { mic_device_name: 'Mikrofon (USB)', mic_device_index: null },
    { mic_device_name: null, mic_device_index: null }, { hotkey: 'ctrl+space', mode: 'press_to_speak' },
    { model_size: 'large-v3-turbo' }, { autostart: true }, { update_check: false }, { first_run_complete: true },
  ];
  for (const partial of samples) {
    assert.deepStrictEqual(filterConfig(partial), { config: partial, rejected: [] });
  }
  // Every key a setup step saves is allowed and has a typed sample above.
  const sampled = new Set(samples.flatMap(Object.keys));
  const calls = [...read('onboarding.html').matchAll(/\bsave\(\{([\s\S]*?)\}\)/g)];
  assert.ok(calls.length >= 5);
  for (const [, body] of calls) {
    for (const [, key] of body.matchAll(/(?:^|,)\s*(\w+):/g)) {
      assert.ok(Object.prototype.hasOwnProperty.call(CONFIG_TYPES, key), `onboarding saves unknown key ${key}`);
      assert.ok(sampled.has(key), `no typed sample for onboarding key ${key}`);
    }
  }
});

test('config: unknown keys and wrong types are dropped by name', () => {
  const r = filterConfig({
    agent_url: 42, hotkey: null, autostart: 'yes', vad_sensitivity: NaN, llm_min_words: Infinity,
    dictionary: [{ from: 'a' }], languages: [{ share: 50 }], evil: 'x', 'a b\nc': 1,
    __proto__: { polluted: true }, toString: 'x', model_size: 'base',
  });
  assert.deepStrictEqual(r.config, { model_size: 'base' });
  assert.deepStrictEqual(r.rejected.sort(), ['a?b?c', 'agent_url', 'autostart', 'dictionary', 'evil',
    'hotkey', 'languages', 'llm_min_words', 'toString', 'vad_sensitivity'].sort());
  assert.strictEqual({}.polluted, undefined);
  const own = filterConfig(JSON.parse('{"__proto__": {"polluted": true}, "hotkey": "f9"}'));
  assert.deepStrictEqual(own, { config: { hotkey: 'f9' }, rejected: ['__proto__'] });
  assert.strictEqual(Object.getPrototypeOf(own.config), Object.prototype);
  assert.ok(filterConfig({ ['x'.repeat(100)]: 1 }).rejected[0].length <= 40);
});

test('config: a payload that is no object is refused whole', () => {
  for (const bad of [null, undefined, 'x', 1, [], [{ hotkey: 'a' }]]) {
    assert.deepStrictEqual(filterConfig(bad), { config: null, rejected: [] });
  }
});
