import pytest

from postprocess import _word_overlap, rule_clean

# ── rule_clean ──


@pytest.mark.parametrize("text, expected", [
    ("Ähm, das ist gut", "Das ist gut"),
    ("das ist äh gut", "das ist gut"),
    ("Das ist ähm gut.", "Das ist gut."),
    ("Hmm, okay", "Okay"),
    ("So the uh deployment failed", "So the deployment failed"),
    ("Erm, I think so", "I think so"),
    ("Also ehm ja", "Also ja"),
    ("Ja öhm nein", "Ja nein"),
    ("Mhm, verstanden", "Verstanden"),
    ("Ähhh, also", "Also"),
])
def test_fillers_removed(text, expected):
    assert rule_clean(text) == expected


@pytest.mark.parametrize("text", [
    "Das ist ähnlich",       # filler inside a word
    "Um 8 Uhr geht es los",  # German "um" is a word
    "Bähm, das war laut",
])
def test_words_containing_fillers_kept(text):
    assert rule_clean(text) == text


def test_stutter_of_three_or_more_collapses():
    assert rule_clean("Das ist sehr sehr sehr sehr gut") == "Das ist sehr gut"
    assert rule_clean("Ich ich ich weiß nicht") == "Ich weiß nicht"


def test_two_repeats_are_kept():
    assert rule_clean("Das ist sehr sehr gut") == "Das ist sehr sehr gut"


def test_spacing_normalised():
    assert rule_clean("Das  ist   gut ,  oder ?") == "Das ist gut, oder?"


def test_line_breaks_kept_and_trimmed():
    assert rule_clean("Hallo Anna,  \n  wie geht's") == "Hallo Anna,\nwie geht's"
    assert rule_clean("Eins\n\nZwei") == "Eins\n\nZwei"


def test_leading_line_break_kept():
    assert rule_clean("\nNeuer Absatz") == "\nNeuer Absatz"


def test_leading_punctuation_removed():
    assert rule_clean(", und dann") == "und dann"


def test_lowercase_source_stays_lowercase():
    assert rule_clean("äh das ist gut") == "das ist gut"


def test_capitalization_after_removed_leading_filler_with_newline():
    assert rule_clean("\nÄhm, das") == "\nDas"


def test_empty_and_filler_only():
    assert rule_clean("") == ""
    assert rule_clean("Ähm.") == ""


# ── _word_overlap ──

def test_identical_text_full_overlap():
    assert _word_overlap("Das ist ein Test", "Das ist ein Test.") == 1.0


def test_empty_output_zero_overlap():
    assert _word_overlap("Das ist ein Test", "") == 0.0
    assert _word_overlap("Das ist ein Test", "...") == 0.0


def test_overlap_is_case_insensitive():
    assert _word_overlap("das ist gut", "Das Ist GUT") == 1.0


def test_translation_has_low_overlap():
    assert _word_overlap("Ich gehe morgen einkaufen", "I am going shopping tomorrow") == 0.0


def test_answer_has_low_overlap():
    src = "wie spät ist es in tokio"
    out = "In Tokio ist es gerade kurz nach drei Uhr nachmittags."
    assert _word_overlap(src, out) < 0.5


def test_rejoined_compounds_count_as_kept():
    assert _word_overlap("noch mal im home office", "nochmal im Homeoffice") == 1.0


def test_short_rejoined_fragments_do_not_count():
    # "ab" (< 4 chars) only occurs inside "abc" joined, not as a word.
    assert _word_overlap("a bc", "ab") == 0.0


def test_partial_overlap_fraction():
    assert _word_overlap("eins zwei", "eins zwei drei vier") == 0.5
