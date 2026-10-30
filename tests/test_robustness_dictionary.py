import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="notype-test-")
os.environ["APPDATA"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP

import pytest  # noqa: E402

import _robustness_stubs  # noqa: E402,F401  (puts the repo on sys.path)
import dictionary  # noqa: E402


def _apply(text, *pairs):
    return dictionary.apply(text, [{"from": f, "to": t} for f, t in pairs])


@pytest.mark.parametrize("text, expected", [
    ("Ich schreibe C sharp und c ist ein Buchstabe.", "Ich schreibe C# und c ist ein Buchstabe."),
    ("Plan C bleibt Plan C.", "Plan C bleibt Plan C."),
    ("C# bleibt C#.", "C# bleibt C#."),  # no "C##"
])
def test_punctuation_target_is_not_shrunk_to_letter(text, expected):
    assert _apply(text, ("C sharp", "C#")) == expected


def test_punctuation_in_heard_phrase_is_literal():
    assert _apply("c plus plus und c", ("c++", "C++")) == "c plus plus und c"
    assert _apply("ich mag c++ sehr", ("c++", "C++")) == "ich mag C++ sehr"
    assert _apply("dot net und net", ("dot net", ".NET")) == ".NET und net"
    assert _apply("ASP.NET", ("dot net", ".NET")) == "ASP.NET"


def test_spacing_and_hyphen_variants_still_match():
    for text in ("Kuber Netis", "Kuber-Netis", "kubernetis", "kuber  netis"):
        assert _apply(text, ("kuber netis", "Kubernetes")) == "Kubernetes"


def test_idempotent_with_punctuation():
    entries = [{"from": "C sharp", "to": "C#"}, {"from": "node js", "to": "Node.js"}]
    once = dictionary.apply("C sharp und node js", entries)
    assert once == "C# und Node.js"
    assert dictionary.apply(once, entries) == once


def test_punctuation_only_entry_is_ignored():
    assert _apply("a # b", ("#", "hash")) == "a # b"


@pytest.mark.parametrize("heard, expected", [
    ("kuber netis.", "kuber netis"),     # end of a sentence, several words
    ('"kuber netis",', "kuber netis"),   # quotes and comma around it
    ("(kuber netis)", "kuber netis"),
    ("Dr.", "Dr."),                      # one word: maybe an abbreviation
    ("z.B.", "z.B."),
    ("Yahoo!", "Yahoo!"),
    ("C#", "C#"),                        # symbols are never trimmed
    ("c++", "c++"),
    (".NET", ".NET"),
    ("Dr. Müller.", "Dr. Müller"),       # inner punctuation stays
    ("node.js,", "node.js"),
    ("Hans'", "Hans'"),                  # apostrophes stay
])
def test_heard_phrase_edge_punctuation(heard, expected):
    assert dictionary._trim_heard(heard) == expected


def test_heard_phrase_with_sentence_period_matches_mid_sentence():
    assert _apply("kuber netis ist toll", ("kuber netis.", "Kubernetes")) == "Kubernetes ist toll"


def test_abbreviation_keeps_its_period():
    assert _apply("Dr. Müller und Dr Who", ("Dr.", "Doktor")) == "Doktor Müller und Dr Who"
