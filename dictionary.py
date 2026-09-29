"""
NoType – Personal Dictionary (learned corrections)

Pairs "heard → meant", e.g. "Kuber Netis" → "Kubernetes" or "Mio Marketing"
→ "Mijo Marketing". Taught once in the settings (from a recent dictation or
by hand), applied to every dictation – deterministic, ~0 ms, independent of
the speech engine, the cleanup mode and the LLM.

Config shape: "dictionary": [{"from": "kuber netis", "to": "Kubernetes"}, ...]

The target spellings double as hints: they are appended to Whisper's
vocabulary prompt and handed to the LLM cleanup as preferred spellings.
"""

import re

MAX_ENTRIES = 500
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# (entries key, compiled regex, replacement per group) – rebuilt only when
# the dictionary changes.
_cache = (None, None, [])


def _body(phrase: str) -> str | None:
    """Regex for a phrase that tolerates spacing/hyphen variants:
    "kuber netis" also matches "Kuber-Netis", "Kubernetis", "kuber  netis"."""
    words = _WORD_RE.findall(phrase)
    if not words:
        return None
    return r"[\s\-]*".join(re.escape(w) for w in words)


def _normalize(entries) -> tuple:
    pairs = []
    for e in (entries or [])[:MAX_ENTRIES]:
        if not isinstance(e, dict):
            continue
        heard, meant = str(e.get("from", "")).strip(), str(e.get("to", "")).strip()
        # Case-only fixes ("whatsapp" → "WhatsApp") are legitimate entries.
        if heard and meant and heard != meant:
            pairs.append((heard, meant))
    return tuple(pairs)


def _resolve(key) -> list:
    """Chains ("Mio" → "Mijo", "Mijo" → "Mijo Marketing") resolve to their
    final spelling up front, so one pass gives the same result as two."""
    rule_of = {}
    for heard, meant in key:
        rule_of.setdefault((_body(heard) or "").lower(), meant)

    def final(meant: str) -> str:
        seen = set()
        while True:
            b = (_body(meant) or "").lower()
            if b in seen or b not in rule_of:
                return meant
            seen.add(b)
            meant = rule_of[b]

    return [(heard, final(meant)) for heard, meant in key]


def _compiled(entries):
    global _cache
    key = _normalize(entries)
    if _cache[0] == key:
        return _cache[1], _cache[2]

    # Every target spelling is also an alternative that maps to itself. With
    # longest-first ordering this protects already-correct text, so applying
    # the dictionary twice (before and after cleanup) never chains or doubles
    # up – "Mijo" → "Mijo Marketing" stays "Mijo Marketing", not "… Marketing
    # Marketing".
    resolved = _resolve(key)

    # Explicit rules first, then the protections for their final spellings.
    alts = {}
    for phrase, meant in [(h, m) for h, m in resolved] + [(m, m) for _, m in resolved]:
        body = _body(phrase)
        if body and body.lower() not in alts:
            alts[body.lower()] = (body, meant, len(phrase))
    if not alts:
        _cache = (key, None, [])
        return None, []

    ordered = sorted(alts.values(), key=lambda a: -a[2])
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(f"(?P<r{i}>{body})" for i, (body, _, _) in enumerate(ordered)) + r")(?!\w)",
        re.IGNORECASE | re.UNICODE,
    )
    targets = [meant for _, meant, _ in ordered]
    _cache = (key, pattern, targets)
    return pattern, targets


def apply(text: str, entries) -> str:
    """Replace every learned mistake in `text` with its correct spelling."""
    if not text or not entries:
        return text
    pattern, targets = _compiled(entries)
    if pattern is None:
        return text
    return pattern.sub(lambda m: targets[int(m.lastgroup[1:])], text)


def terms(entries) -> list[str]:
    """Distinct final spellings, in dictionary order."""
    seen, out = set(), []
    for _, meant in _resolve(_normalize(entries)):
        if meant.lower() not in seen:
            seen.add(meant.lower())
            out.append(meant)
    return out


def hint_terms(vocab: str, entries) -> list[str]:
    """The user's comma-separated vocabulary, then learned spellings that
    aren't in it yet – case-insensitively distinct. The hand-curated
    vocabulary always comes first."""
    seen, out = set(), []
    for term in [p.strip() for p in (vocab or "").split(",")] + terms(entries):
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def whisper_prompt(vocab: str, entries, budget: int = 850) -> str | None:
    """hint_terms() joined within Whisper's prompt budget (~224 tokens ≈ 850
    characters)."""
    joined = ""
    for term in hint_terms(vocab, entries):
        candidate = f"{joined}, {term}" if joined else term
        if len(candidate) > budget:
            break
        joined = candidate
    return joined or None
