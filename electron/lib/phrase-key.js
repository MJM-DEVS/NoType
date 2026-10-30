// Dictionary entries count as the same mistake when their phrase keys match.
// Tokens like dictionary.py's _body(): words and single punctuation marks,
// hyphens and spacing ignored. So "Kuber-Netis" equals "kuber netis", but
// "C#" stays apart from "C". A phrase without a word has no key.
// Loaded by main.js (require) and settings.html (<script src>).
(function (root) {
  const TOKEN_RE = /[\p{L}\p{N}_]+|[^\p{L}\p{N}_\s-]/gu;
  const WORD_RE = /[\p{L}\p{N}_]/u;

  function phraseKey(s) {
    const text = String(s).toLowerCase();
    if (!WORD_RE.test(text)) return '';
    return (text.match(TOKEN_RE) || []).join(' ');
  }

  if (typeof module === 'object' && module.exports) module.exports = { phraseKey };
  else root.NoTypePhraseKey = phraseKey;
})(this);
