const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { loadSettingsFile } = require('../lib/settings-file');

const dirs = [];
test.after(() => dirs.forEach((d) => fs.rmSync(d, { recursive: true, force: true })));

function tmpSettings(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'notype-settings-'));
  dirs.push(dir);
  const file = path.join(dir, 'settings.json');
  for (const [suffix, content] of Object.entries(files)) fs.writeFileSync(file + suffix, content, 'utf-8');
  return file;
}

const good = (hotkey) => JSON.stringify({ hotkey, mode: 'hold_to_speak' });

test('a valid settings.json is used as is', () => {
  const file = tmpSettings({ '': good('ctrl+shift+space'), '.bak1': good('alt+x') });
  const r = loadSettingsFile(file);
  assert.strictEqual(r.config.hotkey, 'ctrl+shift+space');
  assert.strictEqual(r.source, file);
  assert.strictEqual(r.corrupt, null);
  assert.ok(!fs.existsSync(file + '.corrupt'));
});

test('a broken settings.json is kept as .corrupt and the first good backup promoted', () => {
  // An unquoted value: V8's SyntaxError message quotes this part of the input.
  const broken = '{"hotkey": "ctrl+shift+space", "agent_token": secret}';
  assert.throws(() => JSON.parse(broken), (e) => e.message.includes('secret'));
  const file = tmpSettings({ '': broken, '.bak1': '{oops', '.bak2': good('alt+y'), '.bak3': good('alt+z') });
  const r = loadSettingsFile(file);
  assert.strictEqual(r.config.hotkey, 'alt+y');
  assert.strictEqual(r.source, file + '.bak2');
  assert.strictEqual(fs.readFileSync(file + '.corrupt', 'utf-8'), broken);
  assert.strictEqual(JSON.parse(fs.readFileSync(file, 'utf-8')).hotkey, 'alt+y');
  // The error never quotes the file (it can hold the agent token).
  assert.strictEqual(r.error, 'SyntaxError');
});

test('a UTF-8 BOM is skipped, like config.py does', () => {
  const file = tmpSettings({ '': '﻿' + good('alt+m'), '.bak1': good('alt+x') });
  const r = loadSettingsFile(file);
  assert.strictEqual(r.config.hotkey, 'alt+m');
  assert.strictEqual(r.source, file);
  assert.strictEqual(r.error, null);
  assert.ok(!fs.existsSync(file + '.corrupt'));
});

test('a settings.json that cannot be read is left alone, the backup used from memory', () => {
  const file = tmpSettings({ '.bak1': good('alt+r') });
  // A directory stands in for a locked file: it exists, reading it fails.
  fs.mkdirSync(file);
  const r = loadSettingsFile(file);
  assert.strictEqual(r.config.hotkey, 'alt+r');
  assert.strictEqual(r.source, file + '.bak1');
  assert.strictEqual(r.error, 'EISDIR');
  assert.strictEqual(r.corrupt, null);
  assert.ok(!fs.existsSync(file + '.corrupt'));
  assert.ok(fs.statSync(file).isDirectory());
});

test('like config.py, an object without a hotkey does not count', () => {
  const file = tmpSettings({ '': '[]', '.bak1': '{"mode": "x"}', '.bak': good('alt+l') });
  const r = loadSettingsFile(file);
  assert.strictEqual(r.source, file + '.bak');
  assert.strictEqual(r.config.hotkey, 'alt+l');
});

test('nothing usable: no config, broken file still kept', () => {
  const file = tmpSettings({ '': 'garbage' });
  const r = loadSettingsFile(file);
  assert.strictEqual(r.config, null);
  assert.strictEqual(fs.readFileSync(file + '.corrupt', 'utf-8'), 'garbage');
  assert.strictEqual(fs.readFileSync(file, 'utf-8'), 'garbage');
});

test('a missing settings.json is restored from a backup', () => {
  const file = tmpSettings({ '.bak1': good('alt+b') });
  const r = loadSettingsFile(file);
  assert.strictEqual(r.config.hotkey, 'alt+b');
  assert.strictEqual(r.error, null);
  assert.ok(fs.existsSync(file));
});

test('no files at all: no config, no error', () => {
  const file = tmpSettings({});
  assert.deepStrictEqual(loadSettingsFile(file), { config: null, source: null, corrupt: null, error: null });
});
