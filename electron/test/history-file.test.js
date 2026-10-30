const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { loadHistoryFile, saveHistoryFile, debouncedSave } = require('../lib/history-file');

const dirs = [];
test.after(() => dirs.forEach((d) => fs.rmSync(d, { recursive: true, force: true })));

function tmpHistory(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'notype-history-'));
  dirs.push(dir);
  const file = path.join(dir, 'history.json');
  if (content !== undefined) fs.writeFileSync(file, content, 'utf-8');
  return file;
}

const today = new Date().toDateString();
const stored = () => JSON.stringify({
  history: [{ text: 'secret dictation', time: 2 }, { text: 'older one', time: 1 }],
  stats: { [today]: { count: 2, words: 4 } },
});

test('a missing file starts empty and needs no rewrite', () => {
  const r = loadHistoryFile(tmpHistory(), { enabled: false, max: 10 });
  assert.deepStrictEqual(r, { history: [], stats: {}, rewrite: false, error: null });
});

test('switched on: dictations and stats are loaded, capped at max', () => {
  const r = loadHistoryFile(tmpHistory(stored()), { enabled: true, max: 1 });
  assert.deepStrictEqual(r.history, [{ text: 'secret dictation', time: 2 }]);
  assert.deepStrictEqual(r.stats[today], { count: 2, words: 4 });
  assert.strictEqual(r.rewrite, false);
});

test('save with history off writes history: [] and keeps the stats', () => {
  const file = tmpHistory();
  const stats = { [today]: { count: 3, words: 7 } };
  saveHistoryFile(file, { history: [{ text: 'secret dictation', time: 1 }], stats, enabled: false });
  const raw = fs.readFileSync(file, 'utf-8');
  assert.ok(!raw.includes('secret'));
  assert.deepStrictEqual(JSON.parse(raw), { history: [], stats });
  assert.ok(!fs.existsSync(file + '.tmp'));
});

test('loading with history off asks for a rewrite, which drops the text', () => {
  const file = tmpHistory(stored());
  const r = loadHistoryFile(file, { enabled: false, max: 10 });
  assert.deepStrictEqual(r.history, []);
  assert.deepStrictEqual(r.stats[today], { count: 2, words: 4 });
  assert.strictEqual(r.rewrite, true);
  saveHistoryFile(file, { history: r.history, stats: r.stats, enabled: false });
  const data = JSON.parse(fs.readFileSync(file, 'utf-8'));
  assert.deepStrictEqual(data.history, []);
  assert.deepStrictEqual(data.stats[today], { count: 2, words: 4 });
  // Nothing left to drop: the next start does not write again.
  assert.strictEqual(loadHistoryFile(file, { enabled: false, max: 10 }).rewrite, false);
});

test('a broken file is rewritten without text when history is off', () => {
  const broken = '{"history": [{"text": "secret dictation", "time": 1}], "stats": ';
  const file = tmpHistory(broken);
  const r = loadHistoryFile(file, { enabled: false, max: 10 });
  assert.strictEqual(r.rewrite, true);
  assert.strictEqual(r.error, 'SyntaxError');  // never the message, it quotes the file
  saveHistoryFile(file, { history: r.history, stats: r.stats, enabled: false });
  assert.ok(!fs.readFileSync(file, 'utf-8').includes('secret'));
});

test('a broken file is left for the next save when history is on', () => {
  const r = loadHistoryFile(tmpHistory('{oops'), { enabled: true, max: 10 });
  assert.deepStrictEqual(r, { history: [], stats: {}, rewrite: false, error: 'SyntaxError' });
});

test('stats older than 90 days are pruned on save', () => {
  const file = tmpHistory();
  const now = Date.parse('2026-10-30T12:00:00');
  const old = new Date(now - 91 * 24 * 3600 * 1000).toDateString();
  const recent = new Date(now - 89 * 24 * 3600 * 1000).toDateString();
  const stats = { [old]: { count: 1, words: 1 }, [recent]: { count: 1, words: 1 } };
  saveHistoryFile(file, { history: [], stats, enabled: true, now });
  assert.deepStrictEqual(Object.keys(JSON.parse(fs.readFileSync(file, 'utf-8')).stats), [recent]);
});

test('debounced save coalesces, flush writes now and drops the pending one', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let saves = 0;
  const saver = debouncedSave(() => { saves++; }, 1000);
  saver.schedule();
  saver.schedule();
  t.mock.timers.tick(1000);
  assert.strictEqual(saves, 1);
  // Clearing the history flushes: one write now, none left pending.
  saver.schedule();
  saver.flush();
  assert.strictEqual(saves, 2);
  t.mock.timers.tick(5000);
  assert.strictEqual(saves, 2);
});
