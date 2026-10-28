"""
NoType – Spoken punctuation and line breaks

"Wie geht's dir Fragezeichen neue Zeile Viele Grüße" becomes
"Wie geht's dir?\nViele Grüße". Whisper already turns some of these words
into characters on its own ("Komma" → ","), the rest arrive as words, often
with Whisper's own punctuation around them ("Grüße, Ausrufezeichen.").

Commands are matched in the language of the dictation (German, English,
Polish). Words that are also ordinary words ("Punkt", "period", "kropka",
"colon") only count as a command where nothing else makes sense:
  - Whisper set them apart as a sentence of their own ("Gut. Punkt.")
  - another command follows ("Punkt neuer Absatz")
  - they end a dictation Whisper didn't punctuate at all
so "ein wichtiger Punkt." and "um Punkt acht" stay as they are. After an
article ("das Fragezeichen", "a new line") no word is a command.
"""

import re

# kind: "close" attaches to the word before (?!.,:;), "open" to the word
# after ("(", „), "closing" like close but keeps Whisper's punctuation after
# it (")" may well end a sentence), "break" is a line break.
_COMMANDS = {
    "de": {
        "fragezeichen": ("close", "?"),
        "ausrufezeichen": ("close", "!"),
        "doppelpunkt": ("close", ":"),
        "semikolon": ("close", ";"),
        "strichpunkt": ("close", ";"),
        "komma": ("close", ","),
        "punkt": ("period", "."),
        "neue zeile": ("break", "\n"),
        "nächste zeile": ("break", "\n"),
        "zeilenumbruch": ("break", "\n"),
        "neuer absatz": ("break", "\n\n"),
        "nächster absatz": ("break", "\n\n"),
        "klammer auf": ("open", "("),
        "klammer zu": ("closing", ")"),
        "anführungszeichen auf": ("open", "„"),
        "anführungszeichen unten": ("open", "„"),
        "anführungszeichen zu": ("closing", "“"),
        "anführungszeichen oben": ("closing", "“"),
    },
    "en": {
        "question mark": ("close", "?"),
        "exclamation mark": ("close", "!"),
        "exclamation point": ("close", "!"),
        "semicolon": ("close", ";"),
        "comma": ("close", ","),
        "colon": ("period", ":"),
        "period": ("period", "."),
        "full stop": ("period", "."),
        "new line": ("break", "\n"),
        "next line": ("break", "\n"),
        "line break": ("break", "\n"),
        "new paragraph": ("break", "\n\n"),
        "next paragraph": ("break", "\n\n"),
        "open bracket": ("open", "("),
        "open parenthesis": ("open", "("),
        "close bracket": ("closing", ")"),
        "close parenthesis": ("closing", ")"),
        "open quote": ("open", "“"),
        "close quote": ("closing", "”"),
        "end quote": ("closing", "”"),
    },
    "pl": {
        "znak zapytania": ("close", "?"),
        "wykrzyknik": ("close", "!"),
        "dwukropek": ("close", ":"),
        "średnik": ("close", ";"),
        "przecinek": ("close", ","),
        "kropka": ("period", "."),
        "nowa linia": ("break", "\n"),
        "nowy wiersz": ("break", "\n"),
        "następna linia": ("break", "\n"),
        "nowy akapit": ("break", "\n\n"),
        "następny akapit": ("break", "\n\n"),
        "otwórz nawias": ("open", "("),
        "nawias otwierający": ("open", "("),
        "zamknij nawias": ("closing", ")"),
        "nawias zamykający": ("closing", ")"),
        "otwórz cudzysłów": ("open", "„"),
        "zamknij cudzysłów": ("closing", "”"),
    },
}

# After these words the command word is meant literally ("das Komma",
# "eine neue Zeile", "the period", "trial period").
_LITERAL_AFTER = {
    "de": {"der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
           "eines", "kein", "keine", "keinen", "jeden", "jedem", "dieses", "diesen",
           "zum", "am", "um", "beim", "im", "vom", "ins", "wichtiger", "wichtigen",
           "nächsten", "nächster", "letzten", "letzter", "ersten", "erster",
           "guter", "guten", "springende", "springenden"},
    "en": {"the", "a", "an", "this", "that", "same", "each", "every", "per", "trial",
           "grace", "time", "waiting", "notice", "test", "billing", "reporting", "no"},
    "pl": {"ta", "ten", "tę", "jedna", "jedną", "jeden", "czarna", "czarną",
           "każda", "każdy", "ostatnia", "ostatni"},
}

_PUNCT = ",.;:!?"
_SENTENCE_END = ".!?"
# Markers inside the output, resolved at the end:
_CAP = "\x00"    # capitalize the next letter
_GLUE = "\x01"   # no space after this point ("(", „, decimal comma)
_LINE = "\x02"   # line break: next letter capitalized, unless the line
                 # before ends with a comma ("Hallo Anna,\nwie geht's")

_patterns = {}


def _pattern(lang: str):
    if lang not in _patterns:
        phrases = sorted(_COMMANDS[lang], key=len, reverse=True)
        body = "|".join(r"\s+".join(map(re.escape, p.split())) for p in phrases)
        _patterns[lang] = re.compile(
            rf"(?P<pre>[{_PUNCT}]*)(?P<sp>[ \t]*)\b(?P<cmd>{body})\b(?P<post>[{_PUNCT}]*)",
            re.IGNORECASE,
        )
    return _patterns[lang]


def _key(phrase: str) -> str:
    return " ".join(phrase.lower().split())


def apply(text: str, language: str | None) -> str:
    """Turn spoken punctuation in `text` (dictated in `language`) into
    characters. Text in other languages is returned unchanged."""
    lang = (language or "").lower()
    if not text or lang not in _COMMANDS:
        return text
    commands, literal = _COMMANDS[lang], _LITERAL_AFTER[lang]
    pattern = _pattern(lang)

    def next_is_command(rest: str) -> bool:
        m = pattern.match(rest.lstrip(" \t" + _PUNCT))
        return bool(m and m.start("cmd") == 0 and not m.group("pre"))

    def repl(m):
        kind, sym = commands[_key(m.group("cmd"))]
        before = m.string[:m.start()]
        rest = m.string[m.end():]
        prev_word = re.search(r"(\w+)\W*$", before)
        if not m.group("pre") and prev_word and prev_word.group(1).lower() in literal:
            return m.group(0)
        if kind == "period":
            isolated = bool(m.group("pre"))
            followed = next_is_command(rest)
            unpunctuated_end = not rest.strip() and not m.group("post") and not re.search(
                rf"[{_PUNCT}]", before)
            if not (isolated or followed or unpunctuated_end):
                return m.group(0)
            kind = "close"
        pre, post = m.group("pre"), m.group("post")

        # The match swallowed the spaces before the command word, so closing
        # marks attach to the word before; Whisper's own punctuation next to
        # the command gives way to it ("Grüße, Ausrufezeichen." → "Grüße!").
        if kind == "close":
            # Decimal comma between digits: "3 Komma 5" → "3,5".
            if sym == "," and before[-1:].isdigit() and rest.lstrip()[:1].isdigit():
                return "," + _GLUE
            return sym + (_CAP if sym in _SENTENCE_END else "")
        if kind == "closing":
            return sym + post
        if kind == "open":
            return pre + " " + sym + _GLUE
        # Line break: Whisper's punctuation before it stays (the comma after
        # a salutation is real), its punctuation after it goes.
        return pre + sym + _LINE

    out = pattern.sub(repl, text)
    out = re.sub(_GLUE + r"[ \t]*", "", out)
    out = re.sub(r"[ \t]*(\n+" + _LINE + r"?)[ \t]*", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    # German and Polish letters go on in lowercase after "Hallo Anna,";
    # English ones don't.
    if lang != "en":
        out = re.sub(r",(\n+)" + _LINE, r",\1", out)
    out = out.replace(_LINE, _CAP)
    out = re.sub(_CAP + r"+(\W*?)(\w)", lambda m: m.group(1) + m.group(2).upper(), out)
    out = out.replace(_CAP, "")
    return out.strip(" \t")
