import pytest

from correction_watch import MAX_SUGGESTIONS, find_corrections


def fix(inserted, after, prefix="", suffix=""):
    """find_corrections() for a field that held prefix + inserted + suffix."""
    return find_corrections(inserted, prefix + inserted + suffix, after)


def test_readme_example():
    assert fix("Wir nutzen Kuber Netis heute", "Wir nutzen Kubernetes heute") \
        == [("Kuber Netis", "Kubernetes")]


def test_unchanged_field_gives_empty_list():
    assert fix("Das ist ein Test", "Das ist ein Test") == []


def test_inserted_not_in_field_gives_none():
    assert find_corrections("fehlt", "ganz anderer Text", "ganz anderer Text") is None


def test_blank_insertion_gives_none():
    assert find_corrections("   ", "a   b", "a   b") is None


def test_cleared_field_gives_none():
    assert fix("Hallo Welt wie geht es", "") is None


def test_mostly_deleted_dictation_gives_none():
    assert fix("eins zwei drei vier", "eins") is None


def test_text_around_the_dictation_is_ignored():
    before_text, after_text = "Lieber Max, ", " Gruß Anna"
    out = find_corrections("wir nutzen Kuber Netis",
                           before_text + "wir nutzen Kuber Netis" + after_text,
                           "Liebe Maxi, wir nutzen Kubernetes" + " Gruß Annabelle")
    assert out == [("Kuber Netis", "Kubernetes")]


def test_dictation_found_with_prefix():
    assert fix("Kuber Netis läuft jetzt stabil", "Hallo. Kubernetes läuft jetzt stabil",
               prefix="Hallo. ") == [("Kuber Netis", "Kubernetes")]


@pytest.mark.xfail(strict=False, raises=AssertionError, reason=(
    "Bug: replaced words count as removed, so fixing 2 of 3 dictated words "
    "('Kuber Netis' -> 'Kubernetes') looks like 'dictation mostly gone' and "
    "returns None - short dictations never get a suggestion"))
@pytest.mark.parametrize("inserted, after", [
    ("Kuber Netis läuft", "Kubernetes läuft"),
    ("Kuber Netis", "Kubernetes"),
])
def test_multi_word_fix_in_short_dictation(inserted, after):
    assert fix(inserted, after) == [("Kuber Netis", "Kubernetes")]


def test_last_occurrence_is_the_dictation():
    # The same sentence typed earlier in the field stays outside.
    field = "Kuber Netis ist toll. Kuber Netis ist toll"
    after = "Kuber Netis ist toll. Kubernetes ist toll"
    assert find_corrections("Kuber Netis ist toll", field, after) == [("Kuber Netis", "Kubernetes")]


def test_case_only_brand_spelling_counts():
    assert fix("schick es per whatsapp", "schick es per WhatsApp") == [("whatsapp", "WhatsApp")]


def test_acronym_counts():
    assert fix("die api ist down", "die API ist down") == [("api", "API")]


def test_capitalized_sentence_start_does_not_count():
    assert fix("das ist gut", "Das ist gut") == []


def test_separator_fix_counts():
    assert fix("Schreib eine E Mail", "Schreib eine E-Mail") == [("E Mail", "E-Mail")]


def test_lowercase_word_swap_is_grammar():
    assert fix("ich sehe wie es geht", "ich sehe die es geht") == []


def test_dissimilar_replacement_is_a_rewrite():
    assert fix("Wir treffen Peter morgen", "Wir treffen Zygmunt morgen") == []


def test_digit_counts_as_term():
    assert fix("wir nutzen python drei", "wir nutzen python3") == [("python drei", "python3")]


def test_too_short_meant_ignored():
    assert fix("das ist ein Ox Test", "das ist ein OK Test") == []


def test_long_phrase_is_not_a_fix():
    assert fix("eins zwei drei vier fünf sechs sieben acht neun zehn",
               "eins Zwo Drai Fia Fümf sechs sieben acht neun zehn") == []


def test_many_edits_are_a_rewrite():
    inserted = "a1 b2 c3 d4 e5 f6 g7 h8 i9 j10 k11 l12 m13 n14 o15"
    after = "A1 b2 C3 d4 E5 f6 G7 h8 I9 j10 k11 l12 m13 n14 o15"
    assert fix(inserted, after) == []


def test_at_most_max_suggestions():
    inserted = "Alfa x Bravoo x Charly x Deltta x eins zwei drei vier fünf sechs sieben acht"
    after = "Alpha x Bravo x Charlie x Delta x eins zwei drei vier fünf sechs sieben acht"
    out = fix(inserted, after)
    assert len(out) == MAX_SUGGESTIONS
    assert out[0] == ("Alfa", "Alpha")


def test_duplicate_pairs_reported_once():
    inserted = "Kuber Netis und Kuber Netis und noch viel mehr Text hier"
    after = "Kubernetes und Kubernetes und noch viel mehr Text hier"
    assert fix(inserted, after) == [("Kuber Netis", "Kubernetes")]


@pytest.mark.xfail(strict=False, raises=AssertionError, reason=(
    "Bug/limitation: when the fixed word ends the field and the user keeps "
    "typing, the fix and the new text merge into one >3-word replace, so the "
    "correction is lost (docstring: text typed after the dictation stays outside)"))
def test_fix_of_last_word_then_typing_on():
    assert fix("Wir nutzen Kuber Netis", "Wir nutzen Kubernetes. Und noch mehr Text danach") \
        == [("Kuber Netis", "Kubernetes")]
