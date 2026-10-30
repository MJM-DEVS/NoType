from collections import namedtuple

import numpy as np
import pytest

from hallucinations import SAMPLE_RATE, filter_segments, strip_credits

Seg = namedtuple("Seg", "text start end avg_logprob no_speech_prob")


def seg(text, start=0.0, end=1.0, logprob=-0.2, no_speech=0.05):
    return Seg(text, start, end, logprob, no_speech)


def audio_with_levels(*levels, seconds=1.0):
    """A test signal: one constant-amplitude block per level, `seconds` each."""
    n = int(seconds * SAMPLE_RATE)
    return np.concatenate([np.full(n, level, dtype=np.float32) for level in levels])


# ── strip_credits ──

@pytest.mark.parametrize("text", [
    "Untertitel der Amara.org-Community",
    "Untertitel im Auftrag des ZDF für funk, 2017.",
    "Untertitelung des ZDF, 2020",
    "Untertitel von Stephanie Geiges",
    "Copyright WDR 2021",
    "© ZDF 2019",
    "Transcription by CastingWords",
    "Sous-titres réalisés par la communauté d'Amara.org",
    "Subtítulos realizados por la comunidad de Amara.org",
    "Sottotitoli creati dalla comunità Amara.org",
    "Legendas pela comunidade Amara.org",
    "Ondertiteling door de Amara.org gemeenschap",
    "Napisy stworzone przez społeczność Amara.org",
    "Субтитры сделал DimaTorzok",
    "字幕志愿者 李宗盛",
])
def test_credits_removed_completely(text):
    assert strip_credits(text) == ""


def test_chinese_amara_credit_removed_completely():
    assert strip_credits("字幕由Amara.org社区提供") == ""


def test_credit_after_real_sentence_keeps_the_sentence():
    assert strip_credits("Das ist gut. Untertitel im Auftrag des ZDF, 2020.") == "Das ist gut."


def test_credit_does_not_swallow_across_sentence_end():
    out = strip_credits("Wir sehen uns morgen. Untertitel der Amara.org-Community")
    assert out == "Wir sehen uns morgen."


@pytest.mark.parametrize("text", [
    "Die Untertitel sind zu klein.",
    "Ich schaue gern ZDF.",
    "Thanks for watching!",
    "Vielen Dank fürs Zuschauen.",
])
def test_ordinary_text_untouched(text):
    assert strip_credits(text) == text


def test_unchanged_text_keeps_its_spacing():
    assert strip_credits("a  b\nc") == "a  b\nc"


@pytest.mark.parametrize("text", ["", None])
def test_empty_input_returned_as_is(text):
    assert strip_credits(text) == text


def test_credits_are_case_insensitive():
    assert strip_credits("UNTERTITEL DER AMARA.ORG-COMMUNITY") == ""


# ── filter_segments ──

def test_normal_segments_kept_and_stripped():
    segs = [seg("  Hallo zusammen. "), seg("Wie geht's?")]
    assert filter_segments(segs) == ["Hallo zusammen.", "Wie geht's?"]


def test_empty_input():
    assert filter_segments([]) == []


def test_credit_segment_dropped_anywhere():
    segs = [seg("Erster Satz."), seg("Untertitel der Amara.org-Community"), seg("Letzter Satz.")]
    assert filter_segments(segs) == ["Erster Satz.", "Letzter Satz."]


def test_punctuation_only_segment_dropped():
    assert filter_segments([seg("Hallo."), seg("..."), seg(" - ")]) == ["Hallo."]


def test_outro_at_end_with_high_no_speech_dropped():
    segs = [seg("Das ist der Bericht."), seg("Vielen Dank fürs Zuschauen!", no_speech=0.8)]
    assert filter_segments(segs) == ["Das ist der Bericht."]


def test_outro_at_start_with_low_logprob_dropped():
    segs = [seg("Thanks for watching.", logprob=-1.5), seg("Here is the report.")]
    assert filter_segments(segs) == ["Here is the report."]


def test_suspicious_outro_in_the_middle_kept():
    segs = [seg("Erstens."), seg("Danke.", no_speech=0.9), seg("Zweitens.")]
    assert filter_segments(segs) == ["Erstens.", "Danke.", "Zweitens."]


def test_confident_outro_without_audio_kept():
    # A person may really say "Danke." at the end.
    segs = [seg("Bitte schick mir die Datei."), seg("Danke.")]
    assert filter_segments(segs) == ["Bitte schick mir die Datei.", "Danke."]


def test_quiet_outro_dropped_by_loudness():
    audio = audio_with_levels(0.5, 0.01)
    segs = [seg("Bitte schick mir die Datei.", 0.0, 1.0), seg("Danke.", 1.0, 2.0)]
    assert filter_segments(segs, audio) == ["Bitte schick mir die Datei."]


def test_loud_outro_kept_by_loudness():
    audio = audio_with_levels(0.5, 0.4)
    segs = [seg("Bitte schick mir die Datei.", 0.0, 1.0), seg("Danke.", 1.0, 2.0)]
    assert filter_segments(segs, audio) == ["Bitte schick mir die Datei.", "Danke."]


def test_several_outros_in_a_row_peeled_off():
    audio = audio_with_levels(0.5, 0.01, 0.01)
    segs = [seg("Das war alles.", 0.0, 1.0), seg("Vielen Dank.", 1.0, 2.0), seg("Tschüss.", 2.0, 3.0)]
    assert filter_segments(segs, audio) == ["Das war alles."]


def test_single_outro_alone_kept_when_confident():
    assert filter_segments([seg("Thank you.")], audio_with_levels(0.3)) == ["Thank you."]


def test_single_outro_alone_dropped_when_doubted():
    assert filter_segments([seg("Thank you.", no_speech=0.6)]) == []


@pytest.mark.parametrize("text", [
    "Danke für’s Zuschauen!",   # typographic apostrophe
    "THANKS FOR WATCHING",
    "Dziękuję za uwagę.",
    "Merci d'avoir regardé.",
    "Спасибо за просмотр!",
])
def test_outro_normalisation(text):
    assert filter_segments([seg("Inhalt."), seg(text, no_speech=0.9)]) == ["Inhalt."]


def test_non_outro_with_low_confidence_kept():
    # Only known outros are dropped; doubtful real words are Whisper's job.
    segs = [seg("Inhalt."), seg("Das Wetter morgen.", no_speech=0.9)]
    assert filter_segments(segs) == ["Inhalt.", "Das Wetter morgen."]


def test_credit_inside_segment_removed_rest_kept():
    segs = [seg("Bis morgen. Untertitel der Amara.org-Community")]
    assert filter_segments(segs) == ["Bis morgen."]
