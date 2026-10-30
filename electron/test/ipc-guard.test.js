const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const { CHANNEL_PAGES, appPageName, allowSender, senderLabel, filterConfig } = require('../lib/ipc-guard');

const APP = path.resolve(__dirname, '..');
const page = (name, extra = '') => pathToFileURL(path.join(APP, name)).href + extra;
const read = (f) => fs.readFileSync(path.join(APP, f), 'utf-8');

test('page name: files directly in the app folder, query and hash ignored', () => {
  assert.strictEqual(appPageName(page('settings.html'), APP), 'settings.html');
  assert.strictEqual(appPageName(page('settings.html', '#speech'), APP), 'settings.html');
  assert.strictEqual(appPageName(page('overlay.html', '?demo=1&style=amoled#x'), APP), 'overlay.html');
  assert.strictEqual(appPageName(page('lib/x.html'), APP), null);
  assert.strictEqual(appPageName(pathToFileURL(path.join(APP, '..', 'settings.html')).href, APP), null);
  assert.strictEqual(appPageName(pathToFileURL(APP).href, APP), null);
  assert.strictEqual(appPageName(pathToFileURL(APP).href + '/', APP), null);
  assert.strictEqual(appPageName('https://example.com/settings.html', APP), null);
  assert.strictEqual(appPageName('data:text/html,hi', APP), null);
  assert.strictEqual(appPageName('', APP), null);
  assert.strictEqual(appPageName('not a url', APP), null);
  assert.strictEqual(appPageName(page('settings.html'), ''), null);
});

test('page name: Windows app folders in file:///C:/ URLs', () => {
  const dir = 'C:\\Users\\Max Müller\\AppData\\Local\\Programs\\NoType\\resources\\app';
  const base = 'file:///C:/Users/Max%20M%C3%BCller/AppData/Local/Programs/NoType/resources/app';
  assert.strictEqual(appPageName(`${base}/settings.html`, dir), 'settings.html');
  assert.strictEqual(appPageName(`${base}/toast.html?x=1#y`, dir), 'toast.html');
  // Drive letter and folder case differ on Windows only.
  assert.strictEqual(appPageName(`${base.replace('C:', 'c:').replace('Programs', 'programs')}/settings.html`, dir), 'settings.html');
  assert.strictEqual(appPageName(`${base}/settings.html`, dir + '\\'), 'settings.html');
  assert.strictEqual(appPageName('file:///D:/Users/Max%20M%C3%BCller/AppData/Local/Programs/NoType/resources/app/settings.html', dir), null);
  assert.strictEqual(appPageName(`${base}/../settings.html`, dir), null);
  assert.strictEqual(appPageName(`${base}/sub%2Fsettings.html`, dir), null);
  assert.strictEqual(appPageName(`${base}/sub%5Csettings.html`, dir), null);
  assert.strictEqual(appPageName(`${base}/x%2F..%2Fsettings.html`, dir), null);
  assert.strictEqual(appPageName(`${base}/bad%E0%A4%A.html`, dir), null);
  // A UNC install keeps its host.
  assert.strictEqual(appPageName('file://server/share/NoType/settings.html', '\\\\server\\share\\NoType'), 'settings.html');
  assert.strictEqual(appPageName('file://other/share/NoType/settings.html', '\\\\server\\share\\NoType'), null);
});

test('page name: POSIX folders stay case-sensitive', () => {
  assert.strictEqual(appPageName('file:///opt/NoType/settings.html', '/opt/NoType'), 'settings.html');
  assert.strictEqual(appPageName('file:///opt/notype/settings.html', '/opt/NoType'), null);
});

test('senders: each channel only from its pages', () => {
  assert.ok(allowSender('save-config', page('settings.html'), APP));
  assert.ok(allowSender('save-config', page('onboarding.html', '#step2'), APP));
  assert.ok(allowSender('update-action', page('update.html'), APP));
  assert.ok(allowSender('update-action', page('settings.html'), APP));
  assert.ok(allowSender('toast-action', page('toast.html'), APP));
  for (const p of ['toast.html', 'overlay.html', 'update.html', 'suggest.html']) {
    assert.ok(!allowSender('save-config', page(p), APP), p);
    assert.ok(!allowSender('agent-test', page(p), APP), p);
  }
  assert.ok(!allowSender('save-config', 'https://example.com/settings.html', APP));
  assert.ok(!allowSender('save-config', '', APP));
  assert.ok(!allowSender('unknown-channel', page('settings.html'), APP));
  assert.ok(!allowSender('toString', page('settings.html'), APP));
  assert.ok(!allowSender('__proto__', page('settings.html'), APP));
});

test('senders: log label is the page name, never the URL', () => {
  assert.strictEqual(senderLabel(page('toast.html', '?secret=1'), APP), 'toast.html');
  assert.strictEqual(senderLabel('https://example.com/?q=dictation', APP), '(not an app page)');
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
  for (const partial of [
    { app_language: 'de' }, { mic_device_name: 'Mikrofon (USB)', mic_device_index: null },
    { mic_device_name: null, mic_device_index: null }, { hotkey: 'ctrl+space', mode: 'press_to_speak' },
    { model_size: 'large-v3-turbo' }, { autostart: true }, { first_run_complete: true },
  ]) {
    assert.deepStrictEqual(filterConfig(partial), { config: partial, rejected: [] });
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
