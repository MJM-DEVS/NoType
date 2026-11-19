// A model that could not be loaded: classification (mirror of
// speech_engine.load_failure_code), the copy in both languages, and the
// settings defaults that caused it on machines without an NVIDIA GPU.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { TEXT, CODES, failureCode, normalizeFailure, failureText } = require('../lib/model-status');

const FLOAT16_ON_CPU = 'Requested float16 compute type, but the target device or backend do not '
  + 'support efficient float16 computation.';

test('error messages are classified like the backend does', () => {
  // Same cases as tests/test_speech_engine.py::test_load_failure_code.
  const cases = [
    ['Model download failed (Systran/faster-whisper-small): Name or service not known', 'download'],
    ['Loading the model got stuck (no progress for 600 s)', 'stuck'],
    ['Speech engine ended while loading (exit code 3221225477)', 'crashed'],
    ['Could not start the speech engine: [WinError 2]', 'crashed'],
    [`${FLOAT16_ON_CPU} (model small on cpu, tried int8, float32)`, 'compute_type'],
    ['CUDA failed with error out of memory', 'memory'],
    ['std::bad_alloc', 'memory'],
    ['Library cublas64_12.dll is not found or cannot be loaded', 'other'],
    ['', 'other'],
  ];
  for (const [message, code] of cases) assert.strictEqual(failureCode(message), code, message);
});

test('every code has copy in German and English', () => {
  assert.deepStrictEqual(Object.keys(TEXT.de).sort(), Object.keys(TEXT.en).sort());
  for (const lang of ['de', 'en']) {
    for (const code of CODES) assert.ok(TEXT[lang][code], `${lang}.${code}`);
  }
});

test('the hint is a next step, not only "pick another model"', () => {
  for (const lang of ['de', 'en']) {
    assert.match(TEXT[lang].compute_type, /Auto/);
    assert.match(TEXT[lang].other, /Auto/);
    assert.match(TEXT[lang].other, /notype\.log/);
  }
});

test('model_failed data is kept, a bare error message is classified', () => {
  const sent = { code: 'download', message: 'x', model: 'small', device: 'cpu', gpu: null };
  assert.deepStrictEqual(normalizeFailure(sent),
    { code: 'download', message: 'x', model: 'small', device: 'cpu', gpu: null, noGpu: true });
  const bare = normalizeFailure({ message: 'Loading the model got stuck' });
  assert.strictEqual(bare.code, 'stuck');
  assert.strictEqual(bare.noGpu, false);   // a bare error says nothing about the GPU
  assert.strictEqual(normalizeFailure({ code: 'nonsense', message: 'std::bad_alloc' }).code, 'memory');
  assert.strictEqual(normalizeFailure(null).code, 'other');
  // Normalizing twice changes nothing (main.js passes it on to the windows).
  assert.deepStrictEqual(normalizeFailure(normalizeFailure(sent)), normalizeFailure(sent));
  assert.deepStrictEqual(normalizeFailure(bare), bare);
});

test('without an NVIDIA GPU the hint says NoType runs on the processor', () => {
  const noGpu = failureText({ code: 'stuck', device: 'cpu', gpu: null }, 'de');
  assert.ok(noGpu.hint.startsWith(TEXT.de.cpu));
  assert.strictEqual(noGpu.title, TEXT.de.title);
  const en = failureText({ code: 'stuck', device: null, gpu: null }, 'en');
  assert.ok(en.hint.startsWith(TEXT.en.cpu));
  const withGpu = failureText({ code: 'stuck', device: 'cpu', gpu: 'RTX 4070' }, 'en');
  assert.strictEqual(withGpu.hint, TEXT.en.stuck);
  const onCuda = failureText({ code: 'other', device: 'cuda', gpu: null }, 'en');
  assert.strictEqual(onCuda.hint, TEXT.en.other);
  assert.strictEqual(failureText({ message: 'x' }, 'en').hint, TEXT.en.other);
});

test('an unknown language falls back to German', () => {
  assert.strictEqual(failureText({ code: 'memory' }, 'fr').hint, TEXT.de.memory);
});

// ── settings.html: the quantization default ──

const settings = fs.readFileSync(path.join(__dirname, '..', 'settings.html'), 'utf-8');

test('quantization offers Auto first and never defaults to float16', () => {
  const select = settings.match(/<select id="computeSelect">([\s\S]*?)<\/select>/);
  assert.ok(select, 'computeSelect not found');
  const values = [...select[1].matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);
  assert.strictEqual(values[0], 'auto');
  assert.ok(values.includes('float16') && values.includes('int8'));
  assert.doesNotMatch(settings, /compute_type \|\| 'float16'/);
  assert.match(settings, /const want = cfg\.compute_type \|\| 'auto';/);
});

test('the new strings exist in both languages of each page', () => {
  for (const key of ['speech.quant.auto', 'speech.quant.f16', 'speech.quant.i8f16', 'speech.quant.desc']) {
    assert.strictEqual(settings.split(`'${key}':`).length - 1, 2, key);
  }
  const onboarding = fs.readFileSync(path.join(__dirname, '..', 'onboarding.html'), 'utf-8');
  assert.strictEqual(onboarding.split("'s.failed':").length - 1, 2);
  for (const page of [settings, onboarding]) assert.match(page, /<script src="lib\/model-status\.js"><\/script>/);
});

test('the model status badge is not reset by the i18n pass', () => {
  // applyI18n rewrites every [data-i18n] element: with that attribute the
  // badge went back to "Lädt …" after the model was ready.
  assert.match(settings, /<span class="status-badge loading" id="modelStatus">/);
});
