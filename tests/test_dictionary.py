import pytest

import dictionary
from dictionary import MAX_ENTRIES, apply, hint_terms, terms, whisper_prompt


def entry(heard, meant):
    return {"from": heard, "to": meant}


KUBE = [entry("kuber netis", "Kubernetes")]


@pytest.mark.parametrize("text", [
    "Wir nutzen kuber netis",
    "Wir nutzen Kuber Netis",
    "Wir nutzen Kuber-Netis",
    "Wir nutzen Kubernetis",
    "Wir nutzen kuber  netis",
    "Wir nutzen KUBER NETIS",
])
def test_spacing_hyphen_and_case_variants(text):
    assert apply(text, KUBE) == "Wir nutzen Kubernetes"


def test_only_whole_words_match():
    assert apply("kuber netisch", KUBE) == "kuber netisch"
    assert apply("xkuber netis", KUBE) == "xkuber netis"


def test_every_occurrence_replaced():
    assert apply("kuber netis und Kuber Netis", KUBE) == "Kubernetes und Kubernetes"


def test_punctuation_around_match_kept():
    assert apply("(kuber netis), ok.", KUBE) == "(Kubernetes), ok."


def test_correct_spelling_left_alone():
    assert apply("Kubernetes", KUBE) == "Kubernetes"


def test_case_only_entry():
    assert apply("Schick es per whatsapp", [entry("whatsapp", "WhatsApp")]) == "Schick es per WhatsApp"


def test_chain_resolves_to_final_spelling():
    entries = [entry("Mio", "Mijo"), entry("Mijo", "Mijo Marketing")]
    assert apply("Grüße von Mio", entries) == "Grüße von Mijo Marketing"


def test_applying_twice_never_doubles_up():
    entries = [entry("Mio", "Mijo"), entry("Mijo", "Mijo Marketing")]
    once = apply("Mio und Mijo", entries)
    assert once == "Mijo Marketing und Mijo Marketing"
    assert apply(once, entries) == once


def test_longest_phrase_wins():
    entries = [entry("york", "Jork"), entry("new york", "New York")]
    assert apply("in new york", entries) == "in New York"


def test_cyclic_entries_terminate():
    entries = [entry("alpha", "Beta"), entry("beta", "Alpha")]
    assert apply("alpha", entries) in ("Alpha", "Beta")


@pytest.mark.parametrize("entries", [
    None, [], [entry("same", "same")], [entry("", "x")], [entry("x", "  ")],
    ["not a dict"], [{"to": "Nur Ziel"}],
])
def test_invalid_entries_change_nothing(entries):
    assert apply("same x text", entries) == "same x text"


def test_empty_text():
    assert apply("", KUBE) == ""


def test_whitespace_in_entries_trimmed():
    assert apply("kuber netis", [entry("  kuber netis ", " Kubernetes ")]) == "Kubernetes"


def test_cache_follows_dictionary_changes():
    assert apply("foo", [entry("foo", "Bar")]) == "Bar"
    assert apply("foo", [entry("foo", "Baz")]) == "Baz"


def test_entries_beyond_max_ignored():
    entries = [entry(f"w{i}x", f"W{i}Y") for i in range(MAX_ENTRIES)] + [entry("last", "Last")]
    assert apply("last", entries) == "last"
    assert apply("w0x", entries) == "W0Y"


def test_terms_distinct_final_spellings_in_order():
    entries = [entry("kuber netis", "Kubernetes"), entry("kubernetis", "kubernetes"),
               entry("Mio", "Mijo"), entry("Mijo", "Mijo Marketing")]
    assert terms(entries) == ["Kubernetes", "Mijo Marketing"]


def test_hint_terms_vocabulary_first_case_insensitive():
    out = hint_terms("Docker, kubernetes, ,", KUBE + [entry("pie torch", "PyTorch")])
    assert out == ["Docker", "kubernetes", "PyTorch"]


def test_hint_terms_empty():
    assert hint_terms("", []) == []
    assert hint_terms(None, None) == []


def test_whisper_prompt_joins_terms():
    assert whisper_prompt("Docker", KUBE) == "Docker, Kubernetes"


def test_whisper_prompt_respects_budget():
    assert whisper_prompt("aaaa, bbbb, cccc", [], budget=10) == "aaaa, bbbb"


def test_whisper_prompt_none_when_empty():
    assert whisper_prompt("", []) is None
    assert whisper_prompt("toolongterm", [], budget=3) is None


@pytest.mark.xfail(strict=False, raises=AssertionError, reason=(
    "Bug: the self-protection for a target spelling drops its non-word "
    "characters ('.NET' -> body 'NET'), so a second pass turns '.NET' into "
    "'..NET' - contradicts 'applying the dictionary twice never doubles up'"))
def test_target_with_leading_symbol_is_idempotent():
    entries = [entry("dot net", ".NET")]
    once = apply("Wir nutzen dot net", entries)
    assert once == "Wir nutzen .NET"
    assert apply(once, entries) == once


@pytest.mark.xfail(strict=False, raises=AssertionError, reason=(
    "Bug: target 'C++' protects the bare word 'C', so 'Vitamin C' becomes "
    "'Vitamin C++'"))
def test_target_with_trailing_symbol_does_not_grab_prefix_word():
    assert apply("Vitamin C", [entry("c plus plus", "C++")]) == "Vitamin C"


def test_module_has_no_state_leak_between_calls():
    dictionary.apply("x", [entry("x", "Y")])
    assert dictionary.apply("x", []) == "x"
