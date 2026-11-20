// Circuit breaker for the backend process. No electron import: unit-tested
// with `node --test`.
//
// A backend that ends within FAST_MS of its start is a fast crash (a missing
// DLL, a deterministic native crash). Each one doubles the wait before the
// next start (2 s, 4 s, 8 s, 16 s); after GIVE_UP in a row main.js stops
// restarting and shows it. One that ran longer (a watchdog kill after 65 s, a
// crash mid-dictation) starts the count over.
//
// Counting by uptime, not by the time between two exits: the back-off itself
// pushes those apart (16 s after the fourth), so the count reset before it
// could ever reach GIVE_UP and a backend that can't start was restarted
// forever while the tray said "loading".
const FAST_MS = 10_000;
const GIVE_UP = 5;

function afterExit(fastCrashes, uptimeMs) {
  const streak = uptimeMs < FAST_MS ? fastCrashes + 1 : 0;
  return {
    fastCrashes: streak,
    giveUp: streak >= GIVE_UP,
    delay: Math.min(60_000, 2_000 * 2 ** Math.max(0, streak - 1)),
  };
}

module.exports = { FAST_MS, GIVE_UP, afterExit };
