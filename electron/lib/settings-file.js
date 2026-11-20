// Reading settings.json without losing it. No electron import: unit-tested
// with `node --test`.
//
// Same rules as config.py's load_config(): a file counts when it is a JSON
// object with a "hotkey"; otherwise .bak1, .bak2, .bak3 and the legacy .bak
// are tried in that order and the first good one is promoted back to
// settings.json. A broken settings.json is first copied to
// settings.json.corrupt, only so it can be rescued by hand (promoting a
// backup overwrites it, and nothing reads the copy again). Like
// settings.json itself, that copy can hold the agent token.
// A settings.json that exists but cannot be read (locked by a virus scanner
// or a sync client: EBUSY, EPERM, EACCES) is not broken: it is left alone and
// the backup is used from memory only.
const fs = require('fs');

const BACKUP_SUFFIXES = ['.bak1', '.bak2', '.bak3', '.bak'];

// A UTF-8 BOM (Notepad, PowerShell) is not part of the JSON; config.py reads
// with utf-8-sig and skips it too.
function stripBom(text) {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

// The text of settings.json as config.py's decode_settings() reads it: this
// app writes UTF-8, but a file edited by hand can come back as UTF-16
// (PowerShell's `>` and Out-File) or in the ANSI code page (Set-Content in
// Windows PowerShell). Read as UTF-8, the first is no JSON at all and the
// second turns every umlaut into U+FFFD, which the next save makes permanent.
function decodeSettings(buf) {
  if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) return buf.subarray(2).toString('utf16le');
  if (buf.length >= 2 && buf[0] === 0xfe && buf[1] === 0xff) {
    return Buffer.from(buf.subarray(2)).swap16().toString('utf16le');
  }
  if (isUtf8(buf)) return stripBom(buf.toString('utf-8'));
  return new TextDecoder('windows-1252').decode(buf);
}

function isUtf8(buf) {
  try {
    new TextDecoder('utf-8', { fatal: true }).decode(buf);
    return true;
  } catch (_) {
    return false;
  }
}

function readSettingsText(file) {
  return decodeSettings(fs.readFileSync(file));
}

function readUsable(file) {
  let text;
  try {
    text = readSettingsText(file);
  } catch (e) {
    return { error: (e && e.code) || 'unreadable', broken: false };
  }
  try {
    const data = JSON.parse(text);
    if (data && typeof data === 'object' && !Array.isArray(data) && 'hotkey' in data) {
      return { data };
    }
    return { error: 'not a settings object', broken: true };
  } catch (e) {
    // Only the error name: a SyntaxError message quotes the file's content,
    // and settings.json can hold the agent token.
    return { error: (e && e.name) || 'unreadable', broken: true };
  }
}

// Returns { config, source, corrupt, error }. `config` is null when neither
// settings.json nor a backup can be used (the caller falls back to defaults);
// `source` is the file it came from; `corrupt` the path the broken
// settings.json was kept under.
function loadSettingsFile(file) {
  const result = { config: null, source: null, corrupt: null, error: null };
  let promote = true;
  if (fs.existsSync(file)) {
    const main = readUsable(file);
    if (main.data) return { ...result, config: main.data, source: file };
    result.error = main.error;
    if (main.broken) {
      try {
        fs.copyFileSync(file, file + '.corrupt');
        result.corrupt = file + '.corrupt';
      } catch (_) { /* best-effort, the backups are tried anyway */ }
    } else {
      promote = false;
    }
  }
  for (const suffix of BACKUP_SUFFIXES) {
    const bak = file + suffix;
    if (!fs.existsSync(bak)) continue;
    const r = readUsable(bak);
    if (!r.data) continue;
    if (promote) {
      try { fs.copyFileSync(bak, file); } catch (_) { /* used from memory then */ }
    }
    return { ...result, config: r.data, source: bak };
  }
  return result;
}

module.exports = { loadSettingsFile, readSettingsText, decodeSettings, stripBom, BACKUP_SUFFIXES };
