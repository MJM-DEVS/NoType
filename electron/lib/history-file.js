// Reading and writing history.json. No electron import: unit-tested with
// `node --test`.
//
// Stored shape:
//   {
//     "history": [ { text, time }, ... up to MAX_HISTORY ],
//     "stats":   { "Mon May 14 2026": { count, words }, ... }
//   }
// Written atomically (tmp + rename). Robust to corrupt files: if the JSON
// can't be parsed, the caller starts fresh and keeps going.
// With save_history off (`enabled` false) the dictations stay in memory
// until quit and the file keeps the stats only (counts, no text).
const fs = require('fs');

const STATS_DAYS = 90;

// Returns { history, stats, rewrite, error }. `rewrite` is true when the file
// still holds dictation text although saving is off (stored before it was
// switched off, or a broken file whose text can't be told apart): the caller
// writes it again without the text.
function loadHistoryFile(file, { enabled, max }) {
  const result = { history: [], stats: {}, rewrite: false, error: null };
  if (!fs.existsSync(file)) return result;
  let raw;
  try {
    raw = fs.readFileSync(file, 'utf-8');
  } catch (e) {
    // Locked or unreadable: leave it alone, the next save writes it anyway.
    return { ...result, error: (e && e.code) || 'unreadable' };
  }
  try {
    const data = JSON.parse(raw);
    if (enabled && Array.isArray(data.history)) result.history = data.history.slice(0, max);
    if (data.stats && typeof data.stats === 'object') result.stats = data.stats;
    result.rewrite = !enabled && Array.isArray(data.history) && data.history.length > 0;
    return result;
  } catch (e) {
    // Only the error name: a SyntaxError message quotes the file's content,
    // which can be dictated text.
    return { ...result, rewrite: !enabled, error: (e && e.name) || 'unreadable' };
  }
}

// Drops stats older than STATS_DAYS (in place) to keep the file small.
function pruneStats(stats, now = Date.now()) {
  const cutoff = now - STATS_DAYS * 24 * 3600 * 1000;
  for (const key of Object.keys(stats)) {
    const t = Date.parse(key);
    if (!isNaN(t) && t < cutoff) delete stats[key];
  }
  return stats;
}

// Throws when the file can't be written; the caller logs it.
function saveHistoryFile(file, { history, stats, enabled, now }) {
  pruneStats(stats, now);
  const data = { history: enabled ? history : [], stats };
  const tmp = file + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf-8');
  fs.renameSync(tmp, file);
}

// Debounced save: each transcription changes both history and stats, i.e.
// two writes back-to-back. schedule() coalesces them into one write `delay`
// ms later; flush() writes now and drops the pending one.
function debouncedSave(save, delay) {
  let timer = null;
  return {
    schedule() {
      if (timer) return;
      timer = setTimeout(() => { timer = null; save(); }, delay);
    },
    flush() {
      if (timer) { clearTimeout(timer); timer = null; }
      save();
    },
  };
}

module.exports = { loadHistoryFile, saveHistoryFile, pruneStats, debouncedSave, STATS_DAYS };
