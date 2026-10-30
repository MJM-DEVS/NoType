// Who may send what over IPC, and which settings a save may write. No
// electron import: unit-tested with `node --test`.

// Channel -> the pages (HTML files in the app folder) that send it. A message
// from any other page, or on a channel not listed here, is dropped.
const CHANNEL_PAGES = {
  'save-config': ['settings.html', 'onboarding.html'],
  'get-config': ['settings.html', 'onboarding.html'],
  'get-history': ['settings.html'],
  'clear-history': ['settings.html'],
  'list-devices': ['settings.html', 'onboarding.html'],
  'list-models': ['settings.html', 'onboarding.html'],
  'hotkey-capture': ['settings.html', 'onboarding.html'],
  'open-onboarding': ['settings.html'],
  'close-onboarding': ['onboarding.html'],
  'open-settings': ['onboarding.html'],
  'close-settings': ['settings.html'],
  'agent-test': ['settings.html'],
  'ollama-status': ['settings.html'],
  'ollama-setup': ['settings.html'],
  'ollama-update': ['settings.html'],
  'close-toast': ['toast.html'],
  'toast-hover': ['toast.html'],
  'toast-action': ['toast.html'],
  'suggest-hover': ['suggest.html'],
  'suggest-answer': ['suggest.html'],
  'update-hover': ['update.html'],
  'update-action': ['update.html', 'settings.html'],
};

const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

// 'C:\app' and '/opt/app' as they appear in a file: URL path ('/C:/app').
function urlDir(dir) {
  let p = String(dir || '').replace(/\\/g, '/').replace(/\/+$/, '');
  if (/^[A-Za-z]:/.test(p)) p = '/' + p;
  return p;
}

// File name of a page that sits directly in the app folder, else null.
// Query and hash are ignored; Windows paths compare case-insensitively.
function appPageName(url, appDir) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'file:') return null;
    const p = (u.host ? '//' + u.host : '') + decodeURIComponent(u.pathname);
    const i = p.lastIndexOf('/');
    const name = p.slice(i + 1);
    let parent = p.slice(0, i), dir = urlDir(appDir);
    if (!dir || !name || name.includes('\\')) return null;
    if (/^\/[A-Za-z]:(\/|$)|^\/\//.test(dir)) { parent = parent.toLowerCase(); dir = dir.toLowerCase(); }
    return parent === dir ? name : null;
  } catch (_) {
    return null;
  }
}

function allowSender(channel, url, appDir) {
  const name = appPageName(url, appDir);
  return !!name && has(CHANNEL_PAGES, channel) && CHANNEL_PAGES[channel].includes(name);
}

// For the log line of a dropped message: the page name, never the full URL.
function senderLabel(url, appDir) {
  const name = appPageName(url, appDir);
  return name ? name.slice(0, 60) : '(not an app page)';
}

// Settings a page may save, with a rough type each: the defaults in main.js
// and config.py plus what settings.html and onboarding.html send.
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const T = {
  string: (v) => typeof v === 'string',
  bool: (v) => typeof v === 'boolean',
  number: (v) => Number.isFinite(v),
  stringOrNull: (v) => v === null || typeof v === 'string',
  numberOrNull: (v) => v === null || Number.isFinite(v),
  stringOrNumber: (v) => typeof v === 'string' || Number.isFinite(v),
  // [{ from, to }]
  dictionary: (v) => Array.isArray(v) && v.every((e) => isObj(e) && typeof e.from === 'string' && typeof e.to === 'string'),
  // [{ code, share? }]
  languages: (v) => Array.isArray(v) && v.every((e) => isObj(e) && typeof e.code === 'string'
    && (e.share === undefined || e.share === null || Number.isFinite(e.share))),
};

const CONFIG_TYPES = {
  language: T.string,
  languages: T.languages,
  app_language: T.string,
  model_size: T.string,
  hotkey: T.string,
  mode: T.string,
  autostart: T.bool,
  update_check: T.bool,
  auto_language_detect: T.bool,
  spoken_punctuation: T.bool,
  overlay_enabled: T.bool,
  overlay_style: T.string,
  overlay_position: T.string,
  overlay_accent: T.string,
  overlay_theme: T.string,
  overlay_sounds: T.bool,
  initial_prompt: T.string,
  dictionary: T.dictionary,
  learn_corrections: T.bool,
  save_history: T.bool,
  command_enabled: T.bool,
  command_hotkey: T.string,
  agent_enabled: T.bool,
  agent_hotkey: T.string,
  agent_url: T.string,
  agent_token: T.string,
  agent_send_context: T.bool,
  cleanup_mode: T.string,
  llm_model: T.string,
  llm_min_words: T.number,
  insert_method: T.string,
  mic_device_name: T.stringOrNull,
  mic_device_index: T.numberOrNull,
  mute_while_dictating: T.bool,
  ai_context: T.bool,
  live_preview_enabled: T.bool,
  success_toast: T.bool,
  gpu_safe_mode: T.bool,
  first_run_complete: T.bool,
  beam_size: T.stringOrNumber,   // the select sends 'auto' | '1' | '5' …
  compute_type: T.string,
  vad_sensitivity: T.number,
};

// Keeps the known keys with a fitting type. `rejected` holds only key names
// (cut short): the values may be dictation text or a token.
function filterConfig(input) {
  if (!isObj(input)) return { config: null, rejected: [] };
  const config = {}, rejected = [];
  for (const key of Object.keys(input)) {
    if (has(CONFIG_TYPES, key) && CONFIG_TYPES[key](input[key])) config[key] = input[key];
    else rejected.push(key.slice(0, 40).replace(/[^\w.-]/g, '?'));
  }
  return { config, rejected };
}

module.exports = { CHANNEL_PAGES, CONFIG_TYPES, appPageName, allowSender, senderLabel, filterConfig };
