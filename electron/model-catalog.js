// Shared model catalog + card renderer for settings.html and onboarding.html.
// Plain browser script (no require): exposes window.NoTypeModels.
//
// The backend's `list_models` reply adds per-model `downloaded`/`size_mb`
// and the hardware (device, GPU name, VRAM). Everything static about a model
// (name, download size, VRAM need, speed/accuracy) lives here so both pages
// render identical cards.
(function () {
  'use strict';

  // dl_mb = size of the CTranslate2 weights on Hugging Face (first-use download),
  // vram_gb = comfortable float16 budget, speed/acc = 1..5 relative ratings.
  const CATALOG = [
    { id: 'tiny',              name: 'Whisper Tiny',           group: 'whisper', langs: 'multi', dl_mb: 75,   vram_gb: 1,  speed: 5, acc: 1 },
    { id: 'base',              name: 'Whisper Base',           group: 'whisper', langs: 'multi', dl_mb: 145,  vram_gb: 1,  speed: 5, acc: 2 },
    { id: 'small',             name: 'Whisper Small',          group: 'whisper', langs: 'multi', dl_mb: 485,  vram_gb: 2,  speed: 4, acc: 3 },
    { id: 'medium',            name: 'Whisper Medium',         group: 'whisper', langs: 'multi', dl_mb: 1530, vram_gb: 5,  speed: 2, acc: 4 },
    { id: 'large-v2',          name: 'Whisper Large v2',       group: 'whisper', langs: 'multi', dl_mb: 3090, vram_gb: 10, speed: 1, acc: 5 },
    { id: 'large-v3',          name: 'Whisper Large v3',       group: 'whisper', langs: 'multi', dl_mb: 3090, vram_gb: 10, speed: 1, acc: 5 },
    { id: 'large-v3-turbo',    name: 'Whisper Large v3 Turbo', group: 'whisper', langs: 'multi', dl_mb: 1620, vram_gb: 6,  speed: 4, acc: 4 },
    { id: 'distil-large-v3',   name: 'Distil Large v3',        group: 'distil',  langs: 'en',    dl_mb: 1510, vram_gb: 3,  speed: 5, acc: 4 },
    { id: 'distil-large-v3.5', name: 'Distil Large v3.5',      group: 'distil',  langs: 'en',    dl_mb: 1510, vram_gb: 3,  speed: 5, acc: 4 },
    { id: 'german-turbo',      name: 'Large v3 Turbo German',  group: 'special', langs: 'de',    dl_mb: 1620, vram_gb: 6,  speed: 4, acc: 5 },
  ];
  const GROUPS = ['whisper', 'distil', 'special'];

  const T = {
    de: {
      grp: { whisper: 'OpenAI Whisper · 99 Sprachen', distil: 'Distil-Whisper · nur Englisch, schneller', special: 'Spezialisiert' },
      langs: { multi: '99 Sprachen', en: 'Nur Englisch', de: 'Nur Deutsch' },
      download: 'Download', vram: 'VRAM', speed: 'Tempo', acc: 'Genauigkeit',
      rec: 'Empfohlen', active: 'Aktiv', offline: 'Offline bereit', notdl: 'Wird beim ersten Laden heruntergeladen',
      tight: 'Braucht mehr VRAM als deine GPU hat',
      hw_gpu: 'Grafikkarte', hw_cpu: 'Keine NVIDIA-GPU erkannt · CPU-Modus', hw_unknown: 'Hardware wird erkannt …',
      desc: {
        'tiny': 'Sehr schnell, Basis-Qualität – für alte Rechner und kurze Notizen.',
        'base': 'Schnell, brauchbar für kurze Sätze – gut auf CPU-Laptops.',
        'medium': 'Deutlich genauer, aber langsam – für Mittelklasse-GPUs.',
        'small': 'Solide Allround-Wahl auf CPU und älteren GPUs.',
        'large-v2': 'Volle Größe, in manchen Sprachen weniger Halluzinationen als v3.',
        'large-v3': 'Maximale Genauigkeit – braucht eine High-End-GPU.',
        'large-v3-turbo': 'Fast Large-v3-Qualität bei einem Bruchteil der Zeit. Empfehlung für jede moderne NVIDIA-GPU.',
        'distil-large-v3': 'Destilliertes Large v3 – sehr schnell, nur Englisch.',
        'distil-large-v3.5': 'Neueste Distil-Version – beste schnelle Wahl für Englisch.',
        'german-turbo': 'Auf Deutsch feinjustiert – für rein deutsche Diktate (kein Mixed-English).',
      },
      dec: ',',
    },
    en: {
      grp: { whisper: 'OpenAI Whisper · 99 languages', distil: 'Distil-Whisper · English only, faster', special: 'Specialized' },
      langs: { multi: '99 languages', en: 'English only', de: 'German only' },
      download: 'download', vram: 'VRAM', speed: 'Speed', acc: 'Accuracy',
      rec: 'Recommended', active: 'Active', offline: 'Offline ready', notdl: 'Downloaded on first load',
      tight: 'Needs more VRAM than your GPU has',
      hw_gpu: 'Graphics card', hw_cpu: 'No NVIDIA GPU detected · CPU mode', hw_unknown: 'Detecting hardware …',
      desc: {
        'tiny': 'Very fast, basic quality – old machines and quick notes.',
        'base': 'Fast, fine for short sentences – good on CPU-only laptops.',
        'small': 'Solid all-round pick on CPU and older GPUs.',
        'medium': 'Noticeably more accurate but slow – for mid-range GPUs.',
        'large-v2': 'Full size; fewer hallucinations than v3 in some languages.',
        'large-v3': 'Maximum accuracy – needs a high-end GPU.',
        'large-v3-turbo': 'Near Large-v3 quality in a fraction of the time. Recommended for any modern NVIDIA GPU.',
        'distil-large-v3': 'Distilled Large v3 – very fast, English only.',
        'distil-large-v3.5': 'Latest distil release – best fast pick for English.',
        'german-turbo': 'Fine-tuned for German – pure-German dictation (no mixed English).',
      },
      dec: '.',
    },
  };

  const CSS = `
  .model-cards { display: flex; flex-direction: column; gap: 14px; }
  .model-cards .mgrp-title {
    font-size: 10.5px; font-weight: 700; letter-spacing: 0.9px; text-transform: uppercase;
    color: var(--fg-dim); margin: 2px 0 -4px 2px;
  }
  .model-cards .mgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(268px, 1fr)); gap: 10px; }
  .mcard {
    position: relative; cursor: pointer; outline: none;
    background: var(--input-bg); border: 1px solid var(--input-border);
    border-radius: 12px; padding: 12px 14px 11px;
    display: flex; flex-direction: column; gap: 7px;
    transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s;
  }
  .mcard:hover { border-color: var(--input-border-hover); }
  .mcard:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
  .mcard.selected {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent), 0 6px 18px rgba(0, 184, 148, 0.12);
  }
  .mcard.selected::after {
    content: ''; position: absolute; top: 12px; right: 12px;
    width: 8px; height: 8px; border-radius: 50%; background: var(--accent);
    box-shadow: 0 0 0 3px rgba(0, 184, 148, 0.18);
  }
  .mcard-head { display: flex; align-items: center; gap: 8px; padding-right: 16px; flex-wrap: wrap; }
  .mcard-name { font-size: 13px; font-weight: 600; color: var(--fg); }
  .mbadge {
    font-size: 9.5px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase;
    padding: 2px 7px; border-radius: 99px; white-space: nowrap;
  }
  .mbadge.rec { background: rgba(0, 184, 148, 0.14); color: #059669; }
  .mbadge.active { background: rgba(59, 130, 246, 0.14); color: #2563eb; }
  .mbadge.offline { background: var(--hover-bg); color: var(--fg-muted); }
  [data-theme="dark"] .mbadge.rec { background: rgba(0, 212, 170, 0.18); color: #34d399; }
  [data-theme="dark"] .mbadge.active { background: rgba(96, 165, 250, 0.18); color: #93c5fd; }
  .mcard-desc { font-size: 11.5px; line-height: 1.4; color: var(--fg-dim); }
  .mcard-meta { display: flex; flex-wrap: wrap; gap: 6px; }
  .mcard-meta span {
    font-size: 10.5px; color: var(--fg-muted);
    background: var(--hover-bg); border-radius: 6px; padding: 2px 7px;
  }
  .mcard-meta span.tight { color: #d97706; }
  [data-theme="dark"] .mcard-meta span.tight { color: #fbbf24; }
  .mcard-rating { display: flex; align-items: center; gap: 14px; font-size: 10.5px; color: var(--fg-dim); }
  .mcard-rating .rt { display: inline-flex; align-items: center; gap: 5px; }
  .mcard-rating .dots { display: inline-flex; gap: 3px; }
  .mcard-rating .dots b { width: 6px; height: 6px; border-radius: 50%; background: var(--toggle-off); }
  .mcard-rating .dots b.on { background: var(--accent); }
  .mcard-note { font-size: 10.5px; color: var(--fg-dim); }
  .hw-badge {
    font-size: 11px; color: var(--fg-muted); white-space: nowrap; flex-shrink: 0;
    background: var(--hover-bg); border-radius: 8px; padding: 5px 10px;
    display: inline-flex; align-items: center; gap: 6px; max-width: 100%;
    overflow: hidden; text-overflow: ellipsis;
  }
  .hw-badge svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.8; flex: 0 0 auto; }
  `;

  function ensureCss() {
    if (document.getElementById('notype-model-css')) return;
    const st = document.createElement('style');
    st.id = 'notype-model-css';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  function fmtSize(mb, lang) {
    const dec = (T[lang] || T.de).dec;
    if (mb >= 1000) return (mb / 1024).toFixed(1).replace('.', dec) + ' GB';
    return Math.round(mb) + ' MB';
  }

  // Which model to suggest for this machine. Conservative on purpose: the
  // suggestion must load without a VRAM surprise.
  function recommend(info) {
    if (!info) return null;
    if (info.device === 'cuda') {
      const vram = info.gpu && info.gpu.vram_mb;
      if (!vram || vram >= 5500) return 'large-v3-turbo';
      return 'small';
    }
    return 'small';
  }

  function hardwareLabel(info, lang) {
    const L = T[lang] || T.de;
    if (!info) return L.hw_unknown;
    if (info.device === 'cuda' && info.gpu) {
      const name = String(info.gpu.name || 'NVIDIA GPU').replace(/^NVIDIA\s+/i, '');
      const gb = info.gpu.vram_mb ? ' · ' + Math.round(info.gpu.vram_mb / 1024) + ' GB' : '';
      return name + gb;
    }
    if (info.device === 'cuda') return 'NVIDIA GPU (CUDA)';
    return L.hw_cpu;
  }

  function dots(n) {
    const wrap = document.createElement('i');
    wrap.className = 'dots';
    for (let i = 1; i <= 5; i++) {
      const b = document.createElement('b');
      if (i <= n) b.className = 'on';
      wrap.appendChild(b);
    }
    return wrap;
  }

  /**
   * Render the cards into `container`.
   * opts: { lang, selected, active, info, onSelect }
   *   info = backend `models` payload: { device, gpu, models:[{id, downloaded, size_mb}] }
   */
  function render(container, opts) {
    ensureCss();
    const lang = T[opts.lang] ? opts.lang : 'de';
    const L = T[lang];
    const byId = {};
    if (opts.info && Array.isArray(opts.info.models)) {
      for (const m of opts.info.models) byId[m.id] = m;
    }
    const rec = recommend(opts.info);
    const vram = opts.info && opts.info.device === 'cuda' && opts.info.gpu ? opts.info.gpu.vram_mb : null;

    container.innerHTML = '';
    container.classList.add('model-cards');
    container.setAttribute('role', 'radiogroup');

    for (const g of GROUPS) {
      const title = document.createElement('div');
      title.className = 'mgrp-title';
      title.textContent = L.grp[g];
      container.appendChild(title);
      const grid = document.createElement('div');
      grid.className = 'mgrid';
      container.appendChild(grid);

      for (const m of CATALOG.filter(x => x.group === g)) {
        const st = byId[m.id] || {};
        const card = document.createElement('div');
        card.className = 'mcard' + (m.id === opts.selected ? ' selected' : '');
        card.dataset.id = m.id;
        card.tabIndex = 0;
        card.setAttribute('role', 'radio');
        card.setAttribute('aria-checked', m.id === opts.selected ? 'true' : 'false');

        const head = document.createElement('div');
        head.className = 'mcard-head';
        const name = document.createElement('div');
        name.className = 'mcard-name';
        name.textContent = m.name;
        head.appendChild(name);
        if (m.id === rec) head.appendChild(badge('rec', L.rec));
        if (m.id === opts.active) head.appendChild(badge('active', L.active));
        if (st.downloaded) head.appendChild(badge('offline', L.offline));
        card.appendChild(head);

        const desc = document.createElement('div');
        desc.className = 'mcard-desc';
        desc.textContent = L.desc[m.id] || '';
        card.appendChild(desc);

        const meta = document.createElement('div');
        meta.className = 'mcard-meta';
        meta.appendChild(chip(L.langs[m.langs]));
        const sizeMb = st.downloaded && st.size_mb ? st.size_mb : m.dl_mb;
        meta.appendChild(chip(fmtSize(sizeMb, lang) + (st.downloaded ? '' : ' ' + L.download)));
        const tight = vram && m.vram_gb * 1024 > vram;
        const v = chip('~' + m.vram_gb + ' GB ' + L.vram);
        if (tight) { v.classList.add('tight'); v.title = L.tight; }
        meta.appendChild(v);
        card.appendChild(meta);

        const rating = document.createElement('div');
        rating.className = 'mcard-rating';
        rating.appendChild(rt(L.speed, m.speed));
        rating.appendChild(rt(L.acc, m.acc));
        card.appendChild(rating);

        if (!st.downloaded && opts.info) {
          const note = document.createElement('div');
          note.className = 'mcard-note';
          note.textContent = tight ? L.tight : L.notdl;
          card.appendChild(note);
        }

        const pick = () => { if (typeof opts.onSelect === 'function') opts.onSelect(m.id); };
        card.addEventListener('click', pick);
        card.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); }
        });
        grid.appendChild(card);
      }
    }

    function badge(cls, text) {
      const b = document.createElement('span');
      b.className = 'mbadge ' + cls;
      b.textContent = text;
      return b;
    }
    function chip(text) {
      const s = document.createElement('span');
      s.textContent = text;
      return s;
    }
    function rt(label, n) {
      const s = document.createElement('span');
      s.className = 'rt';
      const l = document.createElement('span');
      l.textContent = label;
      s.appendChild(l);
      s.appendChild(dots(n));
      return s;
    }
  }

  function renderHardwareBadge(el, info, lang) {
    el.innerHTML = '';
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', info && info.device === 'cuda'
      ? 'M4 7h16v10H4zM8 17v2M16 17v2M8 11h3v3H8zM13 11h3v3h-3z'
      : 'M6 6h12v12H6zM9 9h6v6H9zM2 10h4M2 14h4M18 10h4M18 14h4M10 2v4M14 2v4M10 18v4M14 18v4');
    svg.appendChild(p);
    el.appendChild(svg);
    const span = document.createElement('span');
    span.textContent = hardwareLabel(info, lang);
    el.appendChild(span);
  }

  function nameOf(id) {
    const m = CATALOG.find(x => x.id === id);
    return m ? m.name : id;
  }

  window.NoTypeModels = { CATALOG, render, renderHardwareBadge, recommend, nameOf, hardwareLabel };
})();
