import pytest

from spoken_punctuation import apply


@pytest.mark.parametrize("text, lang, expected", [
    # The module docstring's own example.
    ("Wie geht's dir Fragezeichen neue Zeile Viele Grüße", "de", "Wie geht's dir?\nViele Grüße"),
    ("Das ist toll Ausrufezeichen", "de", "Das ist toll!"),
    ("Achtung Doppelpunkt alles gut", "de", "Achtung: alles gut"),
    ("erstens Semikolon zweitens", "de", "erstens; zweitens"),
    ("erstens Strichpunkt zweitens", "de", "erstens; zweitens"),
    ("Hallo Komma wie geht's", "de", "Hallo, wie geht's"),
    ("Hello comma how are you question mark", "en", "Hello, how are you?"),
    ("Watch out exclamation mark", "en", "Watch out!"),
    ("Watch out exclamation point", "en", "Watch out!"),
    ("first semicolon second", "en", "first; second"),
    ("Cześć znak zapytania", "pl", "Cześć?"),
    ("Super wykrzyknik", "pl", "Super!"),
    ("Uwaga dwukropek wszystko", "pl", "Uwaga: wszystko"),
    ("raz średnik dwa", "pl", "raz; dwa"),
    ("Cześć przecinek co słychać", "pl", "Cześć, co słychać"),
])
def test_close_marks_attach_to_previous_word(text, lang, expected):
    assert apply(text, lang) == expected


@pytest.mark.parametrize("text, lang, expected", [
    ("Erste Zeile neue Zeile zweite Zeile", "de", "Erste Zeile\nZweite Zeile"),
    ("Eins nächste Zeile zwei", "de", "Eins\nZwei"),
    ("Eins Zeilenumbruch zwei", "de", "Eins\nZwei"),
    ("Eins neuer Absatz zwei", "de", "Eins\n\nZwei"),
    ("Eins nächster Absatz zwei", "de", "Eins\n\nZwei"),
    ("one new line two", "en", "one\nTwo"),
    ("one next line two", "en", "one\nTwo"),
    ("one line break two", "en", "one\nTwo"),
    ("one new paragraph two", "en", "one\n\nTwo"),
    ("one next paragraph two", "en", "one\n\nTwo"),
    ("raz nowa linia dwa", "pl", "raz\nDwa"),
    ("raz nowy wiersz dwa", "pl", "raz\nDwa"),
    ("raz następna linia dwa", "pl", "raz\nDwa"),
    ("raz nowy akapit dwa", "pl", "raz\n\nDwa"),
    ("raz następny akapit dwa", "pl", "raz\n\nDwa"),
])
def test_line_breaks_capitalize_the_next_line(text, lang, expected):
    assert apply(text, lang) == expected


def test_whisper_punctuation_around_command_gives_way():
    # Docstring: "Grüße, Ausrufezeichen." → "Grüße!"
    assert apply("Viele Grüße, Ausrufezeichen.", "de") == "Viele Grüße!"


def test_sentence_end_capitalizes_next_word():
    assert apply("Wie geht's Fragezeichen gut", "de") == "Wie geht's? Gut"


def test_line_break_after_comma_keeps_lowercase_in_german():
    assert apply("Hallo Anna, neue Zeile wie geht's", "de") == "Hallo Anna,\nwie geht's"


def test_line_break_after_comma_capitalizes_in_english():
    assert apply("Hi Anna, new line how are you", "en") == "Hi Anna,\nHow are you"


def test_decimal_comma_between_digits():
    assert apply("Das kostet 3 Komma 5 Euro", "de") == "Das kostet 3,5 Euro"


@pytest.mark.parametrize("text, lang, expected", [
    ("Test Klammer auf wichtig Klammer zu fertig", "de", "Test (wichtig) fertig"),
    ("see open bracket important close bracket done", "en", "see (important) done"),
    ("see open parenthesis x close parenthesis", "en", "see (x)"),
    ("test otwórz nawias ważne zamknij nawias", "pl", "test (ważne)"),
    ("er sagte Anführungszeichen unten hallo Anführungszeichen oben", "de", "er sagte „hallo“"),
    ("er sagte Anführungszeichen auf hallo Anführungszeichen zu", "de", "er sagte „hallo“"),
    ("he said open quote hi close quote", "en", "he said “hi”"),
    ("he said open quote hi end quote", "en", "he said “hi”"),
    ("powiedział otwórz cudzysłów cześć zamknij cudzysłów", "pl", "powiedział „cześć”"),
])
def test_brackets_and_quotes(text, lang, expected):
    assert apply(text, lang) == expected


def test_closing_bracket_keeps_whisper_period_after_it():
    assert apply("Test Klammer auf x Klammer zu.", "de") == "Test (x)."


# "Punkt", "period", "colon", "kropka" only count where nothing else makes sense.

@pytest.mark.parametrize("text, lang", [
    ("Das ist ein wichtiger Punkt.", "de"),
    ("Wir treffen uns um Punkt acht", "de"),
    ("Das ist der springende Punkt", "de"),
    ("That is an important point.", "en"),
    ("The trial period ends today.", "en"),
    ("We are in the grace period", "en"),
    ("Put the colon there", "en"),
    ("To jest ta kropka", "pl"),
])
def test_ordinary_words_stay_words(text, lang):
    assert apply(text, lang) == text


@pytest.mark.parametrize("text, lang", [
    ("Was bedeutet das Fragezeichen", "de"),
    ("Ich brauche eine neue Zeile", "de"),
    ("I want a new line", "en"),
    ("Remove the comma", "en"),
])
def test_after_an_article_nothing_is_a_command(text, lang):
    assert apply(text, lang) == text


@pytest.mark.xfail(strict=False, raises=AssertionError, reason=(
    "Bug: _LITERAL_AFTER['en'] holds words meant for 'period' ('trial period', "
    "'time period', 'no'), but they block every command after them"))
@pytest.mark.parametrize("text, expected", [
    ("No comma thanks", "No, thanks"),
    ("Thank you for your time exclamation mark", "Thank you for your time!"),
    ("This is just a test question mark", "This is just a test?"),
])
def test_period_only_literals_block_other_commands(text, expected):
    assert apply(text, "en") == expected


def test_repeated_command():
    assert apply("Kommst du Fragezeichen Fragezeichen", "de") == "Kommst du??"


def test_period_as_own_sentence():
    assert apply("Gut. Punkt.", "de") == "Gut."


def test_period_before_another_command():
    assert apply("Das war es Punkt neuer Absatz Weiter", "de") == "Das war es.\n\nWeiter"
    assert apply("That was it period new paragraph next", "en") == "That was it.\n\nNext"


@pytest.mark.parametrize("text, lang, expected", [
    ("Das ist gut Punkt", "de", "Das ist gut."),
    ("That is fine period", "en", "That is fine."),
    ("That is fine full stop", "en", "That is fine."),
    ("Note colon", "en", "Note:"),
    ("To jest dobre kropka", "pl", "To jest dobre."),
])
def test_period_ends_unpunctuated_dictation(text, lang, expected):
    assert apply(text, lang) == expected


def test_period_mid_sentence_without_isolation_stays():
    # Whisper punctuated the dictation, so a trailing "Punkt" is a word.
    assert apply("Erstens, das ist der Punkt", "de") == "Erstens, das ist der Punkt"


def test_commands_are_case_insensitive():
    assert apply("Wirklich FRAGEZEICHEN", "de") == "Wirklich?"
    assert apply("one New   Line two", "en") == "one\nTwo"


def test_language_code_is_case_insensitive():
    assert apply("Hallo Fragezeichen", "DE") == "Hallo?"


@pytest.mark.parametrize("language", ["fr", "", None, "xx"])
def test_other_languages_unchanged(language):
    text = "Hallo Fragezeichen neue Zeile"
    assert apply(text, language) == text


def test_commands_only_in_dictation_language():
    # English commands mean nothing in a German dictation.
    assert apply("Hallo question mark", "de") == "Hallo question mark"


def test_empty_text():
    assert apply("", "de") == ""


def test_no_command_text_unchanged():
    assert apply("Ganz normaler Satz, ohne Befehle.", "de") == "Ganz normaler Satz, ohne Befehle."
