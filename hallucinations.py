"""
NoType – Known Whisper hallucinations

Whisper was trained on subtitled video, and on breathing, clicks or room noise
it "remembers" what such files contain: subtitle credits ("Untertitel der
Amara.org-Community", "Untertitel im Auftrag des ZDF") and video outros
("Vielen Dank fürs Zuschauen", "Thanks for watching").

Two tiers:
  credits – never part of a dictation, removed wherever they occur
  outros  – phrases a person might really say ("Danke.", "Bis bald."), so a
            segment that consists only of one is dropped just at the start or
            end of the dictation, and only when the audio says it wasn't
            really spoken: much quieter than the rest of the dictation, or
            flagged by Whisper itself as unlikely speech.
"""

import re
import numpy as np

SAMPLE_RATE = 16000

# Credits: specific enough that a match is never dictated text. Each pattern
# swallows the rest of its sentence ("… des ZDF für funk, 2017.").
_TAIL = r"[^.!?\n。]*[.!?。]?"
_CREDITS_RE = re.compile(
    "(?:" + "|".join([
        # up to a few words before it ("Untertitel der …", "Napisy stworzone
        # przez społeczność …"), but never across a sentence end. Only Latin
        # letters and digits bound the name: CJK sits right next to it
        # ("字幕由Amara.org社区提供").
        r"(?:[^\s.!?]+\s+){0,7}[^\s.!?]*?(?<![a-z0-9_])amara\s?\.?\s?org(?![a-z0-9_])",
        r"\buntertitel(?:ung)?\s+(?:im\s+auftrag\s+)?(?:des|der|von|vom)\s+"
        r"(?:zdf|ard|wdr|ndr|swr|mdr|br|hr|rbb|sr|funk)\b",
        r"untertitel\w*\s*(?:von|:)\s*stephanie\s+geiges",
        r"copyright\s+(?:wdr|ndr|swr|zdf|ard|mdr|br)\b",
        r"©\s*(?:wdr|ndr|swr|zdf|ard|mdr|br)\b",
        r"transcription\s+by\s+castingwords",
        r"sous-titr\w*\s+(?:st\s?'?\s?\d+|société\s+radio-canada|réalisés|par)\b",
        r"subt[íi]tulos\s+(?:realizados|por|hechos)\b",
        r"sottotitoli\s+(?:creati|a\s+cura)\b",
        r"legendas\s+(?:pela|por)\b",
        r"ondertiteling\s+(?:door|ingekort)\b",
        r"tv\s+gelderland\s+\d{4}",
        r"napisy\s+(?:stworzone|wykonane|przygotowa\w*|zrobione|by)\b",
        r"субтитр\w*\s+(?:сделал|создавал|подготовил|делал)",
        r"редактор\s+субтитров",
        r"dimatorzok",
        r"明镜与点点",
        r"字幕(?:由|志愿者)",
    ]) + ")" + _TAIL,
    re.IGNORECASE,
)

# Outros: compared against the whole segment, normalised (lowercase, no
# punctuation). Covers the languages the misdetections end up in, too.
_OUTROS = {
    # de
    "vielen dank fürs zuschauen", "vielen dank für's zuschauen",
    "danke fürs zuschauen", "danke für's zuschauen", "vielen dank fürs zuhören",
    "danke fürs zuhören", "vielen dank für ihre aufmerksamkeit",
    "bis zum nächsten mal", "bis zum nächsten video", "wir sehen uns im nächsten video",
    "das wars für heute", "das war's für heute", "abonniert den kanal",
    "tschüss", "bis bald", "vielen dank", "danke", "danke schön", "dankeschön",
    # en
    "thank you", "thanks", "thank you very much", "thank you so much",
    "thank you for watching", "thanks for watching", "thank you so much for watching",
    "please subscribe", "like and subscribe", "subscribe to my channel",
    "see you next time", "see you in the next video", "bye", "bye bye", "you",
    # pl
    "dziękuję", "dzięki", "dziękuję za uwagę", "dziękuję za obejrzenie",
    "dzięki za obejrzenie", "do zobaczenia", "do zobaczenia w następnym odcinku",
    "zapraszam do subskrypcji", "subskrybuj",
    # fr, es, it, pt, nl, ru, ja
    "merci", "merci d'avoir regardé", "gracias", "gracias por ver",
    "grazie", "grazie per la visione", "obrigado", "obrigado por assistir",
    "bedankt voor het kijken", "спасибо", "спасибо за просмотр",
    "продолжение следует", "ご視聴ありがとうございました",
}

# An outro at the edge counts as not really spoken when it is this much
# quieter than the rest of the dictation …
QUIET_RATIO = 0.3
# … or when Whisper itself doubts it.
NO_SPEECH_SUSPECT = 0.3
LOGPROB_SUSPECT = -0.7

_PUNCT_RE = re.compile(r"[^\w\s']+", re.UNICODE)


def _normalize(text: str) -> str:
    return " ".join(_PUNCT_RE.sub(" ", text.lower().replace("’", "'")).split())


def strip_credits(text: str) -> str:
    """Remove subtitle credits from a piece of text."""
    if not text:
        return text
    cleaned = _CREDITS_RE.sub("", text)
    if cleaned == text:
        return text
    return " ".join(cleaned.split())


def _rms(audio, start: float, end: float) -> float:
    if audio is None:
        return 0.0
    a = audio[max(0, int(start * SAMPLE_RATE)):max(0, int(end * SAMPLE_RATE))]
    return float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0


def filter_segments(segments, audio=None) -> list[str]:
    """Texts of the segments that survive both tiers, in order.

    `segments` are faster-whisper segments (text, start, end, avg_logprob,
    no_speech_prob); `audio` is the 16 kHz signal they were decoded from
    (timestamps in its timeline), used for the loudness check."""
    kept = []
    for seg in segments:
        text = strip_credits(seg.text.strip())
        if re.search(r"\w", text):
            kept.append((seg, text))

    def suspicious(i: int) -> bool:
        seg, text = kept[i]
        if _normalize(text) not in _OUTROS:
            return False
        if seg.no_speech_prob > NO_SPEECH_SUSPECT or seg.avg_logprob < LOGPROB_SUSPECT:
            return True
        others = [_rms(audio, s.start, s.end) for j, (s, _) in enumerate(kept) if j != i]
        if not others or audio is None:
            return False
        return _rms(audio, seg.start, seg.end) < QUIET_RATIO * float(np.median(others))

    # Several in a row ("Vielen Dank. Tschüss.") – peel them off one by one.
    while kept and suspicious(len(kept) - 1):
        kept.pop()
    while kept and suspicious(0):
        kept.pop(0)
    return [text for _, text in kept]
