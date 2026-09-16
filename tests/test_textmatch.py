"""Tests for tekstmatchning.

Regressionstest for de falske positiver der blev fundet mod rigtige data:
'ur' i 'Taburetter', 'sdr' i 'Overtræksdragter', 'rel' i 'varelager'.
"""

import pytest

from auction_hunter.textmatch import distinct_forms, find_keywords, normalize


def test_normalize_folds_danish_characters():
    assert normalize("Højttaler") == "hoejttaler"
    assert normalize("Ærlig Øvelse Å") == "aerlig oevelse aa"
    assert normalize("Bang & Olufsen") == "bang og olufsen"


def test_normalize_strips_punctuation():
    assert normalize("Model: CHG-2/EU") == "model chg 2 eu"


@pytest.mark.parametrize(
    "text,keyword",
    [
        # Falske positiver fundet i rigtige auktionsdata.
        ("3. Stk. Taburetter", "ur"),
        ("Indhold i 2 stk. bure og 1 palle", "ur"),
        ("Overtræksdragter, engangs", "sdr"),
        ("Skab med div. værktøj og varelager", "rel"),
        ("Div. varelager", "rel"),
        ("Horisontal metalbåndsav FMB", "oris"),
    ],
)
def test_substring_does_not_match_inside_word(text, keyword):
    assert find_keywords(text, (keyword,)) == ()


@pytest.mark.parametrize(
    "text,keyword",
    [
        ("Proxmark3 RDV4", "proxmark"),
        ("Raspberry Pi 4", "raspberry pi"),
        ("RaspberryPi 3B+", "raspberrypi"),
        ("SENNHEISER HD 280", "sennheiser"),
        ("Netværksswitches HP ProCurve", "switch"),
        ("Espressomaskine FUTURMAT", "espressomaskine"),
        ("DALI Oberon 5 højttalere", "dali"),
    ],
)
def test_real_matches_still_work(text, keyword):
    assert find_keywords(text, (keyword,)) == (keyword,)


def test_word_boundary_allows_trailing_digits():
    """Mærker skrives ofte med modelnummer direkte efter: Proxmark3."""
    assert find_keywords("Proxmark3", ("proxmark",)) == ("proxmark",)
    assert find_keywords("ESP32 devkit", ("esp32",)) == ("esp32",)


def test_word_boundary_blocks_trailing_letters():
    """'ur' må ikke matche 'ure' eller 'urne' når det står som selvstændigt ord."""
    assert find_keywords("ur til salg", ("ur",)) == ("ur",)
    assert find_keywords("urne", ("ur",)) == ()


class TestDanishCompounds:
    """Dansk sætter ord sammen: 'netværks' + 'switch'. Det skal matches."""

    @pytest.mark.parametrize(
        "text",
        [
            "Netværksswitches HP ProCurve",
            "Rack med netværksswitch",
            "Switches 24 port",
            "Switch ZYXEL",
        ],
    )
    def test_switch_matches_compounds_and_plurals(self, text):
        assert find_keywords(text, ("switch",)) == ("switch",)

    def test_long_keyword_matches_as_suffix_of_compound(self):
        """'ur'-nøgleord som 'armbåndsur' skal stadig matche i sammensætninger."""
        assert find_keywords("Armbåndsur", ("armbåndsur",)) == ("armbåndsur",)
        assert find_keywords("armbaandsur", ("armbaandsur",)) == ("armbaandsur",)
        assert find_keywords("Herreur", ("herreur",)) == ("herreur",)

    def test_short_keyword_does_not_match_inside_compound(self):
        """'ur' er kun 2 tegn og må ikke fange tilfældige orddele."""
        for text in ("Taburetter", "natur", "kultur", "euro", "mursten"):
            assert find_keywords(text, ("ur",)) == (), text

    def test_short_keyword_still_matches_standalone(self):
        assert find_keywords("Flot ur fra 1965", ("ur",)) == ("ur",)

    def test_long_keyword_matches_as_prefix_of_compound(self):
        """'espresso' skal fange 'espressomaskine', som står i konfigurationen."""
        assert find_keywords("Espressomaskine RANCILIO", ("espressomaskine",))
        assert find_keywords("espresso maskine", ("espresso",)) == ("espresso",)

    def test_compound_does_not_bleed_into_unrelated_words(self):
        """Sammensætningsreglen må ikke genindføre de gamle falske positiver."""
        assert find_keywords("Overtræksdragter", ("sdr",)) == ()
        assert find_keywords("varelager", ("rel",)) == ()


def test_accent_folding_matches_unaccented_keyword():
    """Både 'højttaler' og 'hoejttaler' skal fange 'Højttalere'."""
    assert find_keywords("Højttalere sælges", ("hoejttaler",)) == ("hoejttaler",)
    assert find_keywords("Højttalere sælges", ("højttaler",)) == ("højttaler",)


def test_multiple_keywords_returned():
    hits = find_keywords("Yamaha forstærker og højttalere", ("yamaha", "forstærker"))
    assert set(hits) == {"yamaha", "forstærker"}


def test_empty_inputs_are_safe():
    assert find_keywords("", ("a",)) == ()
    assert find_keywords("tekst", ()) == ()
    assert find_keywords("tekst", ("",)) == ()


class TestExactMatching:
    """Mærkenavne der er almindelige orddele må kun matche selvstændigt."""

    def test_exact_blocks_match_inside_word(self):
        """'mission' er et højttalermærke, men 'transmission' er et andet ord."""
        assert find_keywords("Traadloes videotransmission", ("mission",)) == ("mission",)
        assert (
            find_keywords(
                "Traadloes videotransmission", ("mission",), exact=("mission",)
            )
            == ()
        )

    def test_exact_still_matches_standalone(self):
        assert find_keywords(
            "Højttalere Mission QX-2", ("mission",), exact=("mission",)
        ) == ("mission",)

    def test_exact_allows_trailing_punctuation_and_hyphens(self):
        """Mærkenavnet må gerne efterfølges af bindestreg, tal eller komma."""
        for text in ("Mission QX-2", "Mission, sæt", "MISSION 2x"):
            assert find_keywords(text, ("mission",), exact=("mission",)) == (
                "mission",
            ), text

    def test_exact_rejects_plural_that_hides_longer_words(self):
        """'Missions' afvises med vilje — ellers slap 'transmissions' igennem."""
        assert find_keywords("Missions", ("mission",), exact=("mission",)) == ()
        assert find_keywords("transmissions", ("mission",), exact=("mission",)) == ()


class TestDistinctForms:
    """Varianter af samme ord må ikke tælle som to forskellige træffere."""

    @pytest.mark.parametrize(
        "keywords",
        [
            ("strømforsyning", "stroemforsyning"),
            ("højtaler", "højtalere"),
            ("espressomaskine", "espressomaskin"),
            ("switch", "switches"),
        ],
    )
    def test_variants_collapse_to_one_form(self, keywords):
        assert len(distinct_forms(keywords)) == 1

    def test_genuinely_different_words_stay_separate(self):
        forms = distinct_forms(("forstærker", "højttaler", "pladespiller"))
        assert len(forms) == 3

    def test_short_words_keep_their_full_form(self):
        """Korte ord må ikke stemmes til noget meningsløst."""
        assert distinct_forms(("ur",)) == {"ur"}
        assert distinct_forms(("pc",)) == {"pc"}
