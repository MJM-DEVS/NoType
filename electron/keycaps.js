// Hotkeys as keycap chips: one <kbd> per key, labelled the way the key is
// called in the UI language. Settings and the setup wizard share this so a
// combination looks the same everywhere.
(function () {
  const LABELS = {
    de: {
      ctrl: 'Strg', shift: 'Umschalt', alt: 'Alt', win: 'Win', space: 'Leertaste',
      enter: 'Enter', escape: 'Esc', tab: 'Tab', backspace: 'Rücktaste', delete: 'Entf',
      less: '<', greater: '>',
    },
    en: {
      ctrl: 'Ctrl', shift: 'Shift', alt: 'Alt', win: 'Win', space: 'Space',
      enter: 'Enter', escape: 'Esc', tab: 'Tab', backspace: 'Backspace', delete: 'Del',
      less: '<', greater: '>',
    },
  };
  const ALIASES = {
    control: 'ctrl', cmdorctrl: 'ctrl', commandorcontrol: 'ctrl',
    meta: 'win', super: 'win', esc: 'escape', del: 'delete', return: 'enter',
  };

  function keys(hotkey, lang) {
    const names = LABELS[lang] || LABELS.de;
    return String(hotkey || '').split('+')
      .map(p => p.trim().toLowerCase()).filter(Boolean)
      .map(k => {
        k = ALIASES[k] || k;
        return names[k] || k.charAt(0).toUpperCase() + k.slice(1);
      });
  }

  function chip(label) {
    const kbd = document.createElement('kbd');
    kbd.textContent = label;
    return kbd;
  }

  // Replace el's content with the chips of `hotkey`.
  function render(el, hotkey, lang) {
    el.textContent = '';
    for (const label of keys(hotkey, lang)) el.appendChild(chip(label));
  }

  // Text with key placeholders, e.g. "Halte {hotkey} gedrückt. {esc} bricht ab."
  // `slots` maps each placeholder to a hotkey string.
  function fill(el, text, slots, lang) {
    el.textContent = '';
    const names = Object.keys(slots).map(s => s.replace(/[{}]/g, '\\$&'));
    if (!names.length) { el.textContent = text; return; }
    for (const part of String(text).split(new RegExp('(' + names.join('|') + ')'))) {
      if (Object.prototype.hasOwnProperty.call(slots, part)) {
        const group = document.createElement('span');
        group.className = 'keys';
        render(group, slots[part], lang);
        el.appendChild(group);
      } else if (part) {
        el.appendChild(document.createTextNode(part));
      }
    }
  }

  window.NoTypeKeys = { keys, text: (hk, lang) => keys(hk, lang).join(' + '), render, fill };
})();
