"""Tests for linje-for-linje redigering af interests.yml.

Filens kommentarer bærer beslutningerne bag profilen. Et PyYAML-gennemløb
smider dem alle væk — derfor blev ``tools/tune.py`` fjernet fra projektet i
sin tid. Testene her vogter netop den egenskab: en ændring må røre den linje
den skal, og ingen andre.
"""

from __future__ import annotations

import pytest
import yaml

from auction_hunter.web.yamledit import EditError, InterestsFile, find_block


def comment_count(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip().startswith("#"))


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# -- læsning ---------------------------------------------------------------

def test_laeser_kategorier(sample_config):
    categories = InterestsFile(sample_config).categories()
    assert "it_tech" in categories
    assert "audio_hifi" in categories


def test_laeser_noegleord(sample_config):
    keywords = InterestsFile(sample_config).keywords("it_tech", "strong")
    assert "proxmark" in keywords
    assert len(keywords) > 10


def test_laeser_udelukkelser(sample_config):
    excludes = InterestsFile(sample_config).excludes()
    assert "tastatur" in excludes


def test_ukendt_kategori_giver_tom_liste(sample_config):
    assert InterestsFile(sample_config).keywords("findes_ikke", "strong") == []


# -- kommentarer overlever -------------------------------------------------

def test_tilfoej_noegleord_bevarer_kommentarer(sample_config):
    before = comment_count(sample_config)
    handle = InterestsFile(sample_config)
    assert handle.add_keyword("it_tech", "strong", "testord") is True
    handle.save()
    assert comment_count(sample_config) == before


def test_fjern_noegleord_bevarer_kommentarer(sample_config):
    before = comment_count(sample_config)
    handle = InterestsFile(sample_config)
    handle.remove_keyword("it_tech", "strong", "proxmark")
    handle.save()
    assert comment_count(sample_config) == before


def test_aendret_prisloft_bevarer_kommentarer(sample_config):
    before = comment_count(sample_config)
    handle = InterestsFile(sample_config)
    handle.set_scalar(("categories", "it_tech", "max_price"), 9500)
    handle.save()
    assert comment_count(sample_config) == before
    assert yaml.safe_load(read(sample_config))["categories"]["it_tech"]["max_price"] == 9500


def test_tilbagerulning_giver_identisk_fil(sample_config):
    """Tilføj og fjern igen — filen skal være byte-identisk."""
    original = read(sample_config)

    handle = InterestsFile(sample_config)
    handle.add_keyword("it_tech", "strong", "midlertidigt")
    handle.add_exclude("ogsaa_midlertidigt")
    handle.save()

    handle = InterestsFile(sample_config)
    handle.remove_keyword("it_tech", "strong", "midlertidigt")
    handle.remove_exclude("ogsaa_midlertidigt")
    handle.save()

    assert read(sample_config).rstrip() == original.rstrip()


def test_kun_en_linje_aendres_ved_tilfoejelse(sample_config):
    before = read(sample_config).splitlines()
    handle = InterestsFile(sample_config)
    handle.add_keyword("it_tech", "strong", "enkeltlinje")
    handle.save()
    after = read(sample_config).splitlines()
    assert len(after) == len(before) + 1
    # Alle oprindelige linjer skal stadig findes, i samme rækkefølge.
    added = [line for line in after if line not in before]
    assert added == ["      - enkeltlinje"]


# -- korrekthed ------------------------------------------------------------

def test_dublet_tilfoejes_ikke(sample_config):
    handle = InterestsFile(sample_config)
    assert handle.add_keyword("it_tech", "strong", "proxmark") is False


def test_dublet_er_ufoelsom_for_store_bogstaver(sample_config):
    handle = InterestsFile(sample_config)
    assert handle.add_keyword("it_tech", "strong", "PROXMARK") is False


def test_noegleord_normaliseres_til_smaa_bogstaver(sample_config):
    handle = InterestsFile(sample_config)
    handle.add_keyword("it_tech", "strong", "StortOrd")
    handle.save()
    assert "stortord" in InterestsFile(sample_config).keywords("it_tech", "strong")


def test_tilfoejelse_havner_i_det_rigtige_niveau(sample_config):
    handle = InterestsFile(sample_config)
    handle.add_keyword("it_tech", "weak", "kunweak")
    handle.save()

    handle = InterestsFile(sample_config)
    assert "kunweak" in handle.keywords("it_tech", "weak")
    assert "kunweak" not in handle.keywords("it_tech", "strong")
    assert "kunweak" not in handle.keywords("it_tech", "brands")


def test_tilfoejelse_havner_i_den_rigtige_kategori(sample_config):
    handle = InterestsFile(sample_config)
    handle.add_keyword("audio_hifi", "strong", "kunhifi")
    handle.save()

    handle = InterestsFile(sample_config)
    assert "kunhifi" in handle.keywords("audio_hifi", "strong")
    assert "kunhifi" not in handle.keywords("it_tech", "strong")


def test_fjernelse_rammer_kun_det_rigtige_ord(sample_config):
    handle = InterestsFile(sample_config)
    before = handle.keywords("it_tech", "strong")
    handle.remove_keyword("it_tech", "strong", "proxmark")
    handle.save()
    after = InterestsFile(sample_config).keywords("it_tech", "strong")
    assert set(before) - set(after) == {"proxmark"}


def test_fjernelse_af_ukendt_ord_returnerer_false(sample_config):
    assert InterestsFile(sample_config).remove_keyword(
        "it_tech", "strong", "findes-bestemt-ikke"
    ) is False


def test_ukendt_niveau_afvises(sample_config):
    with pytest.raises(EditError, match="Ukendt niveau"):
        InterestsFile(sample_config).add_keyword("it_tech", "ondsindet", "x")


def test_tomt_noegleord_afvises(sample_config):
    with pytest.raises(EditError, match="tomt"):
        InterestsFile(sample_config).add_keyword("it_tech", "strong", "   ")


def test_linjeskift_i_noegleord_afvises(sample_config):
    """Et linjeskift ville kunne skrive vilkårlig YAML ind i filen."""
    with pytest.raises(EditError):
        InterestsFile(sample_config).add_keyword(
            "it_tech", "strong", "ok\n      - injiceret"
        )


def test_for_langt_noegleord_afvises(sample_config):
    with pytest.raises(EditError, match="for langt"):
        InterestsFile(sample_config).add_keyword("it_tech", "strong", "x" * 200)


def test_vaerdi_der_ligner_et_tal_citeres(sample_config):
    handle = InterestsFile(sample_config)
    handle.add_keyword("it_tech", "strong", "2000")
    handle.save()
    data = yaml.safe_load(read(sample_config))
    assert "2000" in data["categories"]["it_tech"]["strong"]


def test_vaerdi_med_kolon_citeres(sample_config):
    handle = InterestsFile(sample_config)
    handle.add_keyword("it_tech", "strong", "model: x1")
    handle.save()
    assert "model: x1" in InterestsFile(sample_config).keywords("it_tech", "strong")


def test_filen_er_gyldig_yaml_efter_hver_aendring(sample_config):
    handle = InterestsFile(sample_config)
    handle.add_keyword("it_tech", "strong", "a")
    handle.add_keyword("audio_hifi", "weak", "b")
    handle.add_exclude("c")
    handle.set_scalar(("categories", "it_tech", "max_price"), 1234)
    handle.save()
    data = yaml.safe_load(read(sample_config))
    assert data["categories"]["it_tech"]["max_price"] == 1234


def test_agenten_kan_stadig_laese_filen(sample_config):
    """Den vigtigste test: config.load_config skal stadig virke bagefter."""
    from auction_hunter.config import load_config

    handle = InterestsFile(sample_config)
    handle.add_keyword("it_tech", "strong", "nyt-testord")
    handle.add_exclude("nyt-udelukket")
    handle.save()

    config = load_config(sample_config)
    category = config.category("it_tech")
    assert category is not None
    assert "nyt-testord" in category.strong
    assert "nyt-udelukket" in config.exclude


def test_udelukkelse_slaar_igennem_i_matcheren(sample_config):
    """En udelukkelse tilføjet fra webben skal faktisk virke."""
    from auction_hunter.config import load_config

    handle = InterestsFile(sample_config)
    handle.add_exclude("frysepose")
    handle.save()

    config = load_config(sample_config)
    assert config.is_excluded("Div. frysepose og køkkenting")


# -- find_block ------------------------------------------------------------

def test_find_block_respekterer_indrykning():
    lines = [
        "categories:",
        "  it_tech:",
        "    strong:",
        "      - a",
        "  audio_hifi:",
        "    strong:",
        "      - b",
    ]
    block = find_block(lines, "it_tech", indent=2)
    assert block is not None
    assert block.start == 1
    assert block.end == 4          # stopper før audio_hifi


def test_find_block_ignorerer_kommentarer_i_afsnittet():
    lines = [
        "exclude:",
        "  # en kommentar",
        "  - a",
        "  - b",
        "",
        "categories:",
    ]
    block = find_block(lines, "exclude", indent=0)
    assert block is not None
    assert block.end == 4          # tom linje trækkes ud af afsnittet


def test_find_block_paa_ukendt_noegle():
    assert find_block(["a:", "  b: 1"], "findes_ikke", indent=0) is None


def test_save_naegter_at_overskrive_en_anden_aendring(sample_config):
    """To samtidige redigeringer maa ikke tabe den ene i stilhed.

    Hele filen laeses i __init__ og skrives tilbage i save(). Uden et tjek
    ville den sidste skrivning vinde og den foerstes noegleord forsvinde uden
    spor. uvicorn koerer sync-ruter i en threadpool, saa to faner er nok.
    """
    en = InterestsFile(sample_config)
    to = InterestsFile(sample_config)

    en.add_keyword("it_tech", "strong", "foerste-ord")
    en.save()

    to.add_keyword("it_tech", "strong", "andet-ord")
    with pytest.raises(EditError, match="ændret siden"):
        to.save()

    # Den foerste aendring skal stadig staa der.
    assert "foerste-ord" in InterestsFile(sample_config).keywords("it_tech", "strong")


def test_save_kan_koere_igen_efter_genindlaesning(sample_config):
    """Efter en genindlaesning skal skrivningen kunne gennemfoeres."""
    en = InterestsFile(sample_config)
    en.add_keyword("it_tech", "strong", "foerste-ord")
    en.save()

    frisk = InterestsFile(sample_config)
    frisk.add_keyword("it_tech", "strong", "andet-ord")
    frisk.save()

    ord = InterestsFile(sample_config).keywords("it_tech", "strong")
    assert "foerste-ord" in ord and "andet-ord" in ord
