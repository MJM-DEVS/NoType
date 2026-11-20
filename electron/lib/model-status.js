// A speech model that could not be loaded: what the tray, the toast, the
// settings and the setup guide say about it, in both languages. The backend
// sends model_failed with a `code` (speech_engine.load_failure_code); an
// older or bare `error: Model load failed: …` is classified here the same way.
// `backend` is this side's own: the backend process itself keeps crashing.
// Loaded by main.js (require), settings.html and onboarding.html (<script src>).
(function (root) {
  const LOG = '%APPDATA%\\NoType\\notype.log';
  const TEXT = {
    de: {
      title: 'Modell lässt sich nicht laden',
      badge: 'Nicht geladen',
      retry: 'Erneut versuchen',
      cpu: 'Keine NVIDIA-Grafikkarte gefunden, NoType läuft auf dem Prozessor.',
      compute_type: 'Die Quantisierung läuft auf diesem Gerät nicht. Unter „Sprache & Modell“ auf „Auto“ stellen und speichern.',
      download: 'Der Download hat nicht geklappt. Internetverbindung prüfen, dann „Erneut versuchen“.',
      stuck: 'Das Laden kam nicht voran. „Erneut versuchen“; passiert es wieder, unter „Sprache & Modell“ ein kleineres Modell wählen.',
      crashed: 'Die Spracherkennung wurde beim Laden beendet. „Erneut versuchen“; passiert es wieder, unter „Sprache & Modell“ Quantisierung „Auto“ oder ein kleineres Modell wählen.',
      memory: 'Zu wenig Arbeitsspeicher für dieses Modell. Unter „Sprache & Modell“ ein kleineres wählen.',
      cpu_unsupported: `Diesem Prozessor fehlt ein Befehlssatz, den die Spracherkennung braucht (z. B. AVX). Details stehen in ${LOG}.`,
      runtime: `Ein Teil der Spracherkennung fehlt oder lässt sich nicht laden. NoType neu installieren (ein Virenscanner kann eine Datei entfernt haben). Details stehen in ${LOG}.`,
      backend: 'Die Spracherkennung startet nicht. „Erneut versuchen“ oder NoType neu starten.',
      other: `Unter „Sprache & Modell“ Quantisierung „Auto“ oder ein kleineres Modell wählen. Details stehen in ${LOG}.`,
    },
    en: {
      title: 'Model could not be loaded',
      badge: 'Not loaded',
      retry: 'Try again',
      cpu: 'No NVIDIA graphics card found, NoType runs on the processor.',
      compute_type: 'This quantization does not run on this device. Set it to “Auto” under “Language & Model” and save.',
      download: 'The download did not work. Check the internet connection, then “Try again”.',
      stuck: 'Loading made no progress. “Try again”; if it happens again, pick a smaller model under “Language & Model”.',
      crashed: 'The speech engine stopped while loading. “Try again”; if it happens again, pick “Auto” quantization or a smaller model under “Language & Model”.',
      memory: 'Not enough memory for this model. Pick a smaller one under “Language & Model”.',
      cpu_unsupported: `This processor lacks an instruction set the speech engine needs (such as AVX). Details are in ${LOG}.`,
      runtime: `A part of the speech engine is missing or can’t be loaded. Reinstall NoType (a virus scanner may have removed a file). Details are in ${LOG}.`,
      backend: 'The speech engine does not start. “Try again” or restart NoType.',
      other: `Pick “Auto” quantization or a smaller model under “Language & Model”. Details are in ${LOG}.`,
    },
  };
  const CODES = ['compute_type', 'download', 'stuck', 'crashed', 'memory', 'cpu_unsupported', 'runtime', 'backend', 'other'];
  // Where loading once more is the next step; for the rest it is a setting.
  const RETRY_FIRST = ['download', 'stuck', 'crashed', 'runtime', 'backend'];

  // Mirror of load_failure_code() in speech_engine.py (exit codes: Windows
  // status as Python reports it, or minus the POSIX signal).
  const CPU_UNSUPPORTED_EXITS = [0xC000001D, -4];
  const RUNTIME_EXITS = [0xC0000135, 0xC0000139, 0xC0000142];
  const RUNTIME_MARKERS = ['dll load failed', 'cannot load library', 'could not load library',
    'cannot be loaded', 'no module named', 'could not start the speech engine', 'onnxruntime'];
  function failureCode(message) {
    const text = String(message || '').toLowerCase();
    const exit = /exit code (-?\d+)/.exec(text);
    const exitCode = exit ? Number(exit[1]) : null;
    if (text.startsWith('model download failed')) return 'download';
    if (text.includes('got stuck')) return 'stuck';
    if (text.includes('illegal instruction') || CPU_UNSUPPORTED_EXITS.includes(exitCode)) return 'cpu_unsupported';
    if (RUNTIME_MARKERS.some((m) => text.includes(m)) || RUNTIME_EXITS.includes(exitCode)) return 'runtime';
    if (text.includes('ended while loading') || text.includes('speech engine is gone')) return 'crashed';
    if (text.includes('compute type')) return 'compute_type';
    if (text.includes('memory') || text.includes('bad_alloc')) return 'memory';
    return 'other';
  }

  // The backend process kept ending right after its start (main.js gave
  // up): what the last end says. `exitCode` as Node reports it (on Windows
  // the unsigned NTSTATUS), `spawnError` the code of a start that failed:
  // backend.exe gone (ENOENT) or blocked (EACCES, EPERM, or UNKNOWN for what
  // libuv has no name for, such as a virus scanner's or an app control
  // policy's refusal). All mean a part of NoType is missing, like a DLL.
  const SPAWN_BLOCKED = ['ENOENT', 'EACCES', 'EPERM', 'UNKNOWN'];
  function backendFailure(exitCode, spawnError) {
    let code = 'backend';
    if (SPAWN_BLOCKED.includes(spawnError) || RUNTIME_EXITS.includes(exitCode)) code = 'runtime';
    else if (CPU_UNSUPPORTED_EXITS.includes(exitCode)) code = 'cpu_unsupported';
    const message = spawnError ? `Backend could not be started (${spawnError})`
      : `Backend exited (exit code ${exitCode})`;
    return { code, message };
  }

  // model_failed data, or what is left of an `error` message → the failure
  // the windows keep until a model loads. `noGpu`: the backend looked for
  // an NVIDIA GPU and found none (unknown for a bare `error`).
  function normalizeFailure(data) {
    const d = data && typeof data === 'object' ? data : {};
    const message = String(d.message || '');
    return {
      code: CODES.includes(d.code) ? d.code : failureCode(message),
      message,
      model: d.model ? String(d.model) : null,
      device: d.device ? String(d.device) : null,
      gpu: d.gpu ? String(d.gpu) : null,
      noGpu: 'noGpu' in d ? d.noGpu === true : 'gpu' in d && !d.gpu,
    };
  }

  // { title, badge, hint, step, retry } in `lang`. Without an NVIDIA GPU the
  // hint starts by saying that NoType runs on the processor; `step` is the
  // next step alone (the toast has two lines for it).
  function failureText(failure, lang) {
    const T = TEXT[lang] || TEXT.de;
    const f = normalizeFailure(failure);
    const step = T[f.code] || T.other;
    const onCpu = f.device !== 'cuda' && f.noGpu && f.code !== 'backend';
    return { title: T.title, badge: T.badge, retry: T.retry, step, hint: onCpu ? `${T.cpu} ${step}` : step };
  }

  // The one button of the failure toast: load again, or the settings.
  function failureAction(failure) {
    return RETRY_FIRST.includes(normalizeFailure(failure).code) ? 'model:retry' : 'settings:speech';
  }

  const api = {
    TEXT, CODES, RETRY_FIRST, failureCode, backendFailure, normalizeFailure, failureText, failureAction,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NoTypeModelStatus = api;
})(this);
