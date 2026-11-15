// Navigation and permission rules for the app's windows. No electron import:
// unit-tested with `node --test`.
const path = require('path');
const { fileURLToPath } = require('url');

// Pages never navigate: every window loads one local file and stays there.
// Only same-document changes inside that file pass (a #hash, a reload).
function isSameDocument(currentUrl, targetUrl) {
  try {
    const a = new URL(currentUrl), b = new URL(targetUrl);
    return a.protocol === 'file:' && b.protocol === 'file:' && a.pathname === b.pathname;
  } catch (_) {
    return false;
  }
}

// A file: URL inside the app folder (the settings page's overlay preview
// iframe loads overlay.html with a query string).
function isAppFile(url, appDir) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'file:') return false;
    const rel = path.relative(path.resolve(appDir), path.resolve(fileURLToPath(u)));
    return !!rel && !rel.startsWith('..') && !path.isAbsolute(rel);
  } catch (_) {
    return false;
  }
}

// Microphone access only for the pages with a mic test, and audio only.
const MIC_PAGES = ['settings.html', 'onboarding.html'];

function allowPermission(permission, pageUrl, details, appDir) {
  if (permission !== 'media') return false;
  const types = (details && details.mediaTypes) || [];
  if (!types.length || types.some((t) => t !== 'audio')) return false;
  if (!isAppFile(pageUrl, appDir)) return false;
  try {
    return MIC_PAGES.includes(path.basename(fileURLToPath(new URL(pageUrl))));
  } catch (_) {
    return false;
  }
}

module.exports = { isSameDocument, isAppFile, allowPermission, MIC_PAGES };
