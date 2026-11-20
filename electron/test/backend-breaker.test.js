// The backend circuit breaker: a backend that can't start (a missing DLL)
// must end in "gave up" – shown as a failure with "Try again" – instead of
// being restarted forever while the tray says "loading".
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { FAST_MS, GIVE_UP, afterExit } = require('../lib/backend-breaker');

// Plays a backend that dies `uptime` ms after every start: the exits, the
// waits in between, and whether the breaker gives up (within `limit` exits).
function run(uptime, limit = 20) {
  let fastCrashes = 0;
  const delays = [];
  for (let exit = 1; exit <= limit; exit++) {
    const r = afterExit(fastCrashes, uptime);
    fastCrashes = r.fastCrashes;
    if (r.giveUp) return { gaveUpAt: exit, delays };
    delays.push(r.delay);
  }
  return { gaveUpAt: null, delays };
}

test('a backend that dies right after its start gives up after five tries', () => {
  for (const uptime of [50, 1500, 3000, FAST_MS - 1]) {
    const { gaveUpAt, delays } = run(uptime);
    assert.strictEqual(gaveUpAt, GIVE_UP, `uptime ${uptime} ms`);
    assert.deepStrictEqual(delays, [2000, 4000, 8000, 16000]);
  }
});

test('the waits between tries do not reset the count', () => {
  // The old rule counted exits less than 10 s apart: the 16 s back-off
  // before the fifth try reset it, and it never gave up.
  const { delays } = run(50);
  assert.ok(delays.some((d) => d > FAST_MS));
  assert.strictEqual(run(50).gaveUpAt, GIVE_UP);
});

test('a backend that ran for a while starts the count over', () => {
  assert.deepStrictEqual(afterExit(3, 65_000), { fastCrashes: 0, giveUp: false, delay: 2000 });
  assert.deepStrictEqual(afterExit(0, FAST_MS), { fastCrashes: 0, giveUp: false, delay: 2000 });
  assert.strictEqual(run(FAST_MS).gaveUpAt, null);   // a watchdog kill now and then is no crash loop
});

test('main.js counts by uptime and shows a backend that gave up as failed', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf-8');
  assert.match(main, /const breaker = afterExit\(consecutiveFastCrashes, Date\.now\(\) - startedAt\);/);
  assert.doesNotMatch(main, /lastBackendDeath/);
  assert.match(main, /if \(breaker\.giveUp\) \{[\s\S]*?const failure = backendFailure\(code, spawnError\);/);
  // Giving up is always told: a toast of its own, or the failure's toast.
  // Before, the first crash's toast ("restarting …") blocked it.
  const giveUp = /if \(breaker\.giveUp\) \{([\s\S]*?)return;/.exec(main)[1];
  assert.doesNotMatch(giveUp, /if \(!crashToastShown\)/);
  assert.match(giveUp, /if \(failure\.code === 'backend'\) showToast\(trayT\(\)\.backendDead/);
  assert.match(giveUp, /setModelFailed\(failure, \{ toast: failure\.code !== 'backend' \}\);/);
  // A failed start is told apart from a crash (the 'error' event of spawn).
  assert.match(main, /if \(String\(err\.syscall \|\| ''\)\.startsWith\('spawn'\)\) spawnError = err\.code/);
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'));
  assert.match(pkg.scripts.check, /node --check lib\/backend-breaker\.js/);
});
