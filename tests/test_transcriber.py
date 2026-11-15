import pytest

from transcriber import normalize_languages, pick_language


def shares(result):
    return {code: round(share, 4) for code, share in result}


# ── normalize_languages ──

@pytest.mark.parametrize("entries", [None, [], [{"code": ""}], ["  "], [42]])
def test_no_languages_falls_back(entries):
    assert normalize_languages(entries) == [("de", 1.0)]


def test_custom_fallback():
    assert normalize_languages([], fallback="en") == [("en", 1.0)]
    assert normalize_languages([], fallback="") == [("de", 1.0)]


def test_plain_codes_split_evenly():
    assert shares(normalize_languages(["de", "en"])) == {"de": 0.5, "en": 0.5}


def test_readme_mix():
    # README: 80 % German, 15 % English, 5 % Polish.
    result = normalize_languages([{"code": "de", "share": 80}, {"code": "en", "share": 15},
                                  {"code": "pl", "share": 5}])
    assert shares(result) == {"de": 0.8, "en": 0.15, "pl": 0.05}
    assert [c for c, _ in result] == ["de", "en", "pl"]


def test_empty_share_gets_what_is_left():
    result = normalize_languages([{"code": "de", "share": 80}, {"code": "en", "share": 15},
                                  {"code": "pl"}])
    assert shares(result) == {"de": 0.8, "en": 0.15, "pl": 0.05}


def test_several_empty_shares_split_the_rest():
    result = normalize_languages([{"code": "de", "share": 60}, {"code": "en"}, {"code": "pl"}])
    assert shares(result) == {"de": 0.6, "en": 0.2, "pl": 0.2}


def test_empty_share_at_least_one_when_nothing_left():
    result = normalize_languages([{"code": "de", "share": 90}, {"code": "en", "share": 20},
                                  {"code": "pl"}])
    assert shares(result) == {"de": round(90 / 111, 4), "en": round(20 / 111, 4),
                              "pl": round(1 / 111, 4)}


def test_shares_need_not_add_up_to_100():
    result = normalize_languages([{"code": "de", "share": 3}, {"code": "en", "share": 1}])
    assert shares(result) == {"de": 0.75, "en": 0.25}


def test_result_sums_to_one_and_is_sorted():
    result = normalize_languages([{"code": "pl", "share": 10}, {"code": "de", "share": 50},
                                  {"code": "en", "share": 40}])
    assert abs(sum(s for _, s in result) - 1.0) < 1e-9
    assert [c for c, _ in result] == ["de", "en", "pl"]


def test_tiny_and_negative_shares_raised_to_one():
    result = normalize_languages([{"code": "de", "share": 99}, {"code": "en", "share": -5}])
    assert shares(result) == {"de": 0.99, "en": 0.01}


def test_invalid_share_treated_as_empty():
    result = normalize_languages([{"code": "de", "share": "viel"}, {"code": "en", "share": 50}])
    assert shares(result) == {"de": 0.5, "en": 0.5}


def test_string_share_is_parsed():
    assert shares(normalize_languages([{"code": "de", "share": "75"}, {"code": "en", "share": "25"}])) \
        == {"de": 0.75, "en": 0.25}


def test_codes_lowercased_and_duplicates_ignored():
    result = normalize_languages([" DE ", {"code": "de", "share": 10}, "En"])
    assert shares(result) == {"de": 0.5, "en": 0.5}


# ── pick_language ──

MIX = [("de", 0.8), ("en", 0.15), ("pl", 0.05)]


def test_close_call_goes_to_main_language():
    # README: a quick "Okay, passt" stays German.
    assert pick_language({"en": 0.5, "de": 0.45}, MIX, True) == "de"


def test_clear_english_stays_english():
    assert pick_language({"en": 0.95, "de": 0.03}, MIX, True) == "en"


def test_unmistakable_other_language_recognized():
    assert pick_language({"ru": 0.9, "de": 0.05}, MIX, True) == "ru"


def test_other_language_weighed_down():
    # 0.5 * 0.2 (other) loses against 0.3 * 0.8 ** 0.3
    assert pick_language({"fr": 0.5, "de": 0.3}, MIX, True) == "de"


def test_other_languages_off_picks_from_list_only():
    assert pick_language({"ru": 0.9, "pl": 0.05}, MIX, False) == "pl"


def test_no_probabilities_gives_first_language():
    assert pick_language({}, MIX, False) == "de"
    assert pick_language({}, MIX, True) == "de"


def test_tie_goes_to_list_order_then_alphabetical():
    # Equal scores must not depend on PYTHONHASHSEED.
    assert pick_language({"pl": 0.0, "en": 0.0}, MIX, True) == "de"
    assert pick_language({"ru": 0.5, "fr": 0.5}, [("de", 1.0)], True) == "fr"
    even = [("en", 0.5), ("de", 0.5)]
    assert pick_language({"de": 0.4, "en": 0.4}, even, True) == "en"
    assert pick_language({"de": 0.4, "en": 0.4}, even, False) == "en"


def test_single_language_without_others():
    assert pick_language({"en": 0.99}, [("de", 1.0)], False) == "de"
