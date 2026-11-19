// A speech model that could not be loaded: what the tray, the toast, the
// settings and the setup guide say about it, in both languages. The backend
// sends model_failed with a `code` (speech_engine.load_failure_code); an
// older or bare `error: Model load failed: …` is classified here the same way.
// Loaded by main.js (require), settings.html and onboarding.html (<script src>).
(function (root) {
  const TEXT = {
    de: {
      title: 'Modell lässt sich nicht laden',
      badge: 'Nicht geladen',
      cpu: 'Keine NVIDIA-Grafikkarte gefunden, NoType läuft auf dem Prozessor.',
      compute_type: 'Die Quantisierung läuft auf diesem Gerät nicht. Unter „Sprache & Modell“ auf „Auto“ stellen und speichern.',
      download: 'Der Download hat nicht geklappt. Internetverbindung prüfen, dann die Einstellungen speichern oder NoType neu starten.',
      stuck: 'Das Laden kam nicht voran. NoType neu starten. Passiert es wieder, unter „Sprache & Modell“ ein kleineres Modell wählen.',
      crashed: 'Die Spracherkennung wurde beim Laden beendet. Unter „Sprache & Modell“ Quantisierung „Auto“ oder ein kleineres Modell wählen.',
      memory: 'Zu wenig Arbeitsspeicher für dieses Modell. Unter „Sprache & Modell“ ein kleineres wählen.',
      other: 'Unter „Sprache & Modell“ Quantisierung „Auto“ oder ein kleineres Modell wählen. Details stehen in %APPDATA%\\NoType\\notype.log.',
    },
    en: {
      title: 'Model could not be loaded',
      badge: 'Not loaded',
      cpu: 'No NVIDIA graphics card found, NoType runs on the processor.',
      compute_type: 'This quantization does not run on this device. Set it to “Auto” under “Language & Model” and save.',
      download: 'The download did not work. Check the internet connection, then save the settings or restart NoType.',
      stuck: 'Loading made no progress. Restart NoType. If it happens again, pick a smaller model under “Language & Model”.',
      crashed: 'The speech engine stopped while loading. Pick “Auto” quantization or a smaller model under “Language & Model”.',
      memory: 'Not enough memory for this model. Pick a smaller one under “Language & Model”.',
      other: 'Pick “Auto” quantization or a smaller model under “Language & Model”. Details are in %APPDATA%\\NoType\\notype.log.',
    },
  };
  const CODES = ['compute_type', 'download', 'stuck', 'crashed', 'memory', 'other'];

  // Mirror of load_failure_code() in speech_engine.py.
  function failureCode(message) {
    const text = String(message || '').toLowerCase();
    if (text.startsWith('model download failed')) return 'download';
    if (text.includes('got stuck')) return 'stuck';
    if (text.includes('ended while loading') || text.includes('could not start the speech engine')
        || text.includes('speech engine is gone')) return 'crashed';
    if (text.includes('compute type')) return 'compute_type';
    if (text.includes('memory') || text.includes('bad_alloc')) return 'memory';
    return 'other';
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

  // { title, badge, hint } in `lang`. Without an NVIDIA GPU the hint starts
  // by saying that NoType runs on the processor.
  function failureText(failure, lang) {
    const T = TEXT[lang] || TEXT.de;
    const f = normalizeFailure(failure);
    const hint = T[f.code] || T.other;
    const onCpu = f.device !== 'cuda' && f.noGpu;
    return { title: T.title, badge: T.badge, hint: onCpu ? `${T.cpu} ${hint}` : hint };
  }

  const api = { TEXT, CODES, failureCode, normalizeFailure, failureText };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NoTypeModelStatus = api;
})(this);
