// Pixel-art sprites for the character overlays (overlay.html) and their
// miniatures in the settings style picker (settings.html). One character
// per art pixel; '.' is transparent. Letters map to palette slots that each
// page resolves (accent-tinted or fixed), see the legend per sprite.
'use strict';
(function () {
  // ── Dragon ─────────────────────────────────────────────────────
  // Faces right; its fire breath is the voice visualizer.
  // K outline · B body · D shade · L belly/chin · H horn · W wing
  // E eye · Q eye glint · N nostril
  // Parts share one coordinate system (22 × 20); `at` is the top-left.
  const dragon = {
    w: 22, h: 20,
    mouth: { x: 21, y: 9 },          // where the fire leaves the snout
    nostril: { x: 20, y: 7 },
    wing: [
      { at: [1, 2], rows: [            // raised
        'K.......',
        'KK......',
        'KWK.....',
        'KWWK....',
        'KWWWK...',
        '.KWWWK..',
        '.KWWWWK.',
        '..KWKWWK',
        '...K.KK.',
      ] },
      { at: [0, 5], rows: [            // lowered
        'KK.......',
        'KWKK.....',
        '.KWWKK...',
        '.KWWWWKK.',
        '..KWWWWWK',
        '...KWKWWK',
        '....K.KK.',
      ] },
    ],
    tail: { at: [0, 12], rows: [
      'K......',
      'KK.....',
      'KBK....',
      '.KBK...',
      '.KBBK..',
      '..KBBBK',
      '...KKKK',
    ] },
    body: { at: [2, 10], rows: [
      '......KDBBK...',
      '.....KDBBBBK..',
      '....KDDBBLLLK.',
      '....KDDBBLLKBK',
      '...KDDBBBLLLKK',
      '...KDDBBBLLLK.',
      '...KDDDBBBLLK.',
      '....KDDDBBBK..',
      '....KBBKKBBK..',
      '....KKKK.KKKK.',
    ] },
    head: { at: [8, 0], rows: [
      '.K..K.........',
      'KHKKHK........',
      'KHHKHHKKK.....',
      'KBBBBBBBBBK...',
      'KBBBBBBBBBBK..',
      'KBBBBBBBBBBBK.',
      'KBBBBBBBBBBBBK',
      'KDBBBBBBBBBBNK',
      'KDBBBBBBBBBBBK',
      'KDDBBBKKKKKKK.',
    ] },
    jaw: { at: [10, 10], rows: [
      '.KDDLLLLLLK',
      '..KKKKKKKK.',
    ] },
    // Mouth opening between the head's lip line and the jaw (dragon x).
    mouthSpan: [14, 19],
    eye: { at: [14, 4], open: ['BB', 'EQ', 'EE', 'EE'], up: ['EQ', 'EE', 'EE', 'BB'], blink: ['BB', 'BB', 'BB', 'KK'] },
    happyEye: { at: [13, 5], rows: ['.K.', 'K.K'] },
  };

  // ── Mimic ──────────────────────────────────────────────────────
  // A treasure chest whose lid is the mouth. Front view, 20 wide.
  // K outline · w light wood · W wood · d plank seam · G gold · g dark gold
  // O keyhole
  const mimic = {
    w: 20,
    lid: [
      '....KKKKKKKKKKKK....',
      '..KKwGwwwwwwwwGwKK..',
      '.KwwwGwwwwwwwwGwwwK.',
      'KwWWWGWWWWWWWWGWWWwK',
      'KWWWWGWWWWWWWWGWWWWK',
      'KddddGddddddddGddddK',
      'KWWWWGWWWWWWWWGWWWWK',
      'KGGGGGGGGGGGGGGGGGGK',
    ],
    base: [
      'KGGGGGGGGGGGGGGGGGGK',
      'KWWWWGWWGGGGWWGWWWWK',
      'KWWWWGWWGOOGWWGWWWWK',
      'KddddGddGGGGddGddddK',
      'KWWWWGWWWWWWWWGWWWWK',
      'KWWWWGWWWWWWWWGWWWWK',
      'KddddGddddddddGddddK',
      'KWWWWGWWWWWWWWGWWWWK',
      'KKKKKKKKKKKKKKKKKKKK',
    ],
    // Inside the mouth (chest x): 2-wide eyes, 3-wide teeth (left edge)
    eyes: [5, 13],
    teethTop: [2, 6, 11, 15],
    teethBottom: [4, 13],
  };

  window.NoTypeSprites = { dragon, mimic };
})();
