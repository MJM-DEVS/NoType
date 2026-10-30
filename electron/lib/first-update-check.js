// The first automatic update check waits until the setup guide is done, so
// its "Check for updates" switch is answered before NoType contacts GitHub.
// Returns a function to call with the config whenever first_run_complete may
// have changed (startup, every save, the guide closing); it calls schedule()
// once, the first time that flag is set. No electron import: unit-tested with
// `node --test`.
function firstUpdateCheck(schedule) {
  let scheduled = false;
  return function maybeSchedule(config) {
    if (scheduled || !config || !config.first_run_complete) return false;
    scheduled = true;
    schedule();
    return true;
  };
}

module.exports = { firstUpdateCheck };
