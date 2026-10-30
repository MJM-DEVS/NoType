const test = require('node:test');
const assert = require('node:assert');
const { phraseKey } = require('../lib/phrase-key');

test('case, spacing and hyphens do not matter', () => {
  assert.strictEqual(phraseKey('Kuber-Netis'), 'kuber netis');
  assert.strictEqual(phraseKey('  kuber   netis '), 'kuber netis');
  assert.strictEqual(phraseKey('Mio Marketing'), phraseKey('mio marketing'));
});

test('punctuation is part of the phrase: C# is not C', () => {
  assert.strictEqual(phraseKey('C#'), 'c #');
  assert.strictEqual(phraseKey('C'), 'c');
  assert.notStrictEqual(phraseKey('C#'), phraseKey('C'));
  assert.notStrictEqual(phraseKey('node.js'), phraseKey('node js'));
});

test('letters beyond ASCII count as words', () => {
  assert.strictEqual(phraseKey('Grüße'), 'grüße');
  assert.strictEqual(phraseKey('Žižek_2'), 'žižek_2');
});

test('a phrase without a word has no key', () => {
  assert.strictEqual(phraseKey('?!'), '');
  assert.strictEqual(phraseKey(' - '), '');
  assert.strictEqual(phraseKey(''), '');
});
