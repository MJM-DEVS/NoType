const test = require('node:test');
const assert = require('node:assert');
const { firstUpdateCheck } = require('../lib/first-update-check');

function counter() {
  const c = { calls: 0 };
  c.maybe = firstUpdateCheck(() => { c.calls += 1; });
  return c;
}

test('an existing install schedules the check at startup', () => {
  const c = counter();
  assert.strictEqual(c.maybe({ first_run_complete: true }), true);
  assert.strictEqual(c.calls, 1);
});

test('the first run waits for the setup guide', () => {
  const c = counter();
  const config = { first_run_complete: false };
  assert.strictEqual(c.maybe(config), false);
  assert.strictEqual(c.calls, 0);
  // The guide's finish() saves first_run_complete before close-onboarding
  // arrives, so the save is the transition; the close must not add a second.
  config.first_run_complete = true;
  assert.strictEqual(c.maybe(config), true);
  assert.strictEqual(c.maybe(config), false);
  assert.strictEqual(c.calls, 1);
});

test('later saves from settings do not schedule again', () => {
  const c = counter();
  c.maybe({ first_run_complete: true });
  for (let i = 0; i < 3; i++) c.maybe({ first_run_complete: true });
  assert.strictEqual(c.calls, 1);
});

test('a missing config does not schedule', () => {
  const c = counter();
  assert.strictEqual(c.maybe(undefined), false);
  assert.strictEqual(c.maybe({}), false);
  assert.strictEqual(c.calls, 0);
});
