// main.js names models in tray and toasts, model-catalog.js on the model
// cards: both lists must stay the same.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { MODEL_LABELS, modelLabel } = require('../lib/model-labels');

function loadCatalog() {
  const window = {};
  const src = fs.readFileSync(path.join(__dirname, '..', 'model-catalog.js'), 'utf-8');
  vm.runInNewContext(src, { window });
  return window.NoTypeModels.CATALOG;
}

test('labels match the model catalog', () => {
  const fromCatalog = Object.fromEntries(loadCatalog().map((m) => [m.id, m.name]));
  assert.deepStrictEqual(MODEL_LABELS, fromCatalog);
});

test('an unknown model id is shown as is', () => {
  assert.strictEqual(modelLabel('small'), 'Whisper Small');
  assert.strictEqual(modelLabel('my-model'), 'my-model');
});
