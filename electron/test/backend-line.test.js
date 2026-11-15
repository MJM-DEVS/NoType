const test = require('node:test');
const assert = require('node:assert');
const { parseBackendLine } = require('../lib/backend-line');

test('a message yields its type and data', () => {
  const r = parseBackendLine('{"type": "transcription_done", "data": {"text": "Hallo Welt"}}');
  assert.deepStrictEqual(r, { type: 'transcription_done', data: { text: 'Hallo Welt' } });
});

test('a message without data', () => {
  assert.deepStrictEqual(parseBackendLine('{"type": "pong"}'), { type: 'pong', data: undefined });
});

test('non-JSON and typeless lines are rejected without echoing the line', () => {
  for (const line of ['geheimer Diktattext', '{"data": {"text": "geheimer Diktattext"}}', '["x"]', 'null', '{"type": 3}']) {
    const r = parseBackendLine(line);
    assert.ok(r.error, line);
    assert.strictEqual(r.type, undefined);
    assert.ok(!r.error.includes('geheim'), r.error);
    assert.ok(r.error.includes(`${line.length} chars`), r.error);
  }
});
