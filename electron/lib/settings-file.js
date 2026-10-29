// Reading settings.json without losing it. No electron import: unit-tested
// with `node --test`.
//
// Same rules as config.py's load_config(): a file counts when it is a JSON
// object with a "hotkey"; otherwise .bak1, .bak2, .bak3 and the legacy .bak
// are tried in that order and the first good one is promoted back to
// settings.json. A broken settings.json is first kept as settings.json.corrupt,
// because the next save rotates the current file into .bak1 and would push a
// good backup out with a broken copy.
const fs = require('fs');

const BACKUP_SUFFIXES = ['.bak1', '.bak2', '.bak3', '.bak'];

function readUsable(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, 'utf-8'));
    if (data && typeof data === 'object' && !Array.isArray(data) && 'hotkey' in data) {
      return { data };
    }
    return { error: 'not a settings object' };
  } catch (e) {
    // Only the error name: a SyntaxError message quotes the file's content,
    // and settings.json can hold the agent token.
    return { error: e && e.code ? e.code : (e && e.name) || 'unreadable' };
  }
}

// Returns { config, source, corrupt, error }. `config` is null when neither
// settings.json nor a backup can be used (the caller falls back to defaults);
// `source` is the file it came from; `corrupt` the path the broken
// settings.json was kept under.
function loadSettingsFile(file) {
  const result = { config: null, source: null, corrupt: null, error: null };
  if (fs.existsSync(file)) {
    const main = readUsable(file);
    if (main.data) return { ...result, config: main.data, source: file };
    result.error = main.error;
    try {
      fs.copyFileSync(file, file + '.corrupt');
      result.corrupt = file + '.corrupt';
    } catch (_) { /* best-effort, the backups are tried anyway */ }
  }
  for (const suffix of BACKUP_SUFFIXES) {
    const bak = file + suffix;
    if (!fs.existsSync(bak)) continue;
    const r = readUsable(bak);
    if (!r.data) continue;
    try { fs.copyFileSync(bak, file); } catch (_) { /* used from memory then */ }
    return { ...result, config: r.data, source: bak };
  }
  return result;
}

module.exports = { loadSettingsFile, BACKUP_SUFFIXES };
