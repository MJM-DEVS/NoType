const test = require('node:test');
const assert = require('node:assert');
const path = require('path');
const { pathToFileURL } = require('url');
const { isSameDocument, isAppFile, allowPermission } = require('../lib/guards');

const APP = path.resolve(__dirname, '..');
const page = (name, extra = '') => pathToFileURL(path.join(APP, name)).href + extra;

test('same document: query/hash changes on the same file pass', () => {
  assert.ok(isSameDocument(page('settings.html'), page('settings.html', '#speech')));
  assert.ok(isSameDocument(page('overlay.html'), page('overlay.html', '?demo=1')));
});

test('navigating to another page, a website or a bad URL is blocked', () => {
  assert.ok(!isSameDocument(page('settings.html'), page('onboarding.html')));
  assert.ok(!isSameDocument(page('settings.html'), 'https://example.com/'));
  assert.ok(!isSameDocument(page('settings.html'), 'javascript:alert(1)'));
  assert.ok(!isSameDocument('', page('settings.html')));
});

test('app files: inside the app folder only', () => {
  assert.ok(isAppFile(page('overlay.html', '?demo=1&style=amoled'), APP));
  assert.ok(!isAppFile(pathToFileURL(path.join(APP, '..', 'secret.html')).href, APP));
  assert.ok(!isAppFile(pathToFileURL(APP).href, APP));
  assert.ok(!isAppFile('https://example.com/overlay.html', APP));
  assert.ok(!isAppFile('not a url', APP));
});

test('microphone: audio for settings and setup only', () => {
  const audio = { mediaTypes: ['audio'] };
  assert.ok(allowPermission('media', page('settings.html'), audio, APP));
  assert.ok(allowPermission('media', page('onboarding.html'), audio, APP));
  assert.ok(!allowPermission('media', page('overlay.html'), audio, APP));
  assert.ok(!allowPermission('media', page('toast.html'), audio, APP));
  assert.ok(!allowPermission('media', 'https://example.com/settings.html', audio, APP));
  assert.ok(!allowPermission('media', page('settings.html'), { mediaTypes: ['audio', 'video'] }, APP));
  assert.ok(!allowPermission('media', page('settings.html'), {}, APP));
  assert.ok(!allowPermission('notifications', page('settings.html'), audio, APP));
  assert.ok(!allowPermission('clipboard-read', page('settings.html'), {}, APP));
});
