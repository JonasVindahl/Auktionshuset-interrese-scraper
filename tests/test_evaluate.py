"""Tests for facitliste-porten.

Facitlisten er den versionerede beslutning om hvad der er interessant, saa en
ændring skal kunne vurderes mod den uden at skrive noget.
"""

from __future__ import annotations

from auction_hunter.config import Category, Config, Source
from auction_hunter.evaluate import apply_edit, corpus_impact, hard_failures


def _config(*, strong=("switch",), weak=()) -> Config:
    return Config(
        source=Source(),
        categories=(
            Category(key="it_tech", label="IT", emoji="", max_price=1000,
                     strong=strong, weak=weak),
        ),
        max_price=1000,
        soft_over_budget_factor=2.5,
    )


CASES = [
    {"title": "Switch TP-LINK TL-SG1016D", "expect": "yes", "why": "netvaerksswitch"},
    {"title": "Div. havemøbler i teak", "expect": "no", "why": "ikke et fund"},
    {"title": "Måske et eller andet", "expect": "maybe", "why": "graensetilfaelde"},
]


def test_hard_failures_ser_kun_yes_og_no():
    # Uden noegleord matcher intet: yes-sagen fejler, no-sagen bestaar, maybe ignoreres.
    assert list(hard_failures(_config(strong=()), CASES)) == ["Switch TP-LINK TL-SG1016D"]


def test_apply_edit_kan_fjerne_og_tilfoeje():
    fjernet = apply_edit(_config(), action="remove", category="it_tech",
                         level="strong", keyword="switch")
    assert fjernet is not None and fjernet.categories[0].strong == ()

    tilfoejet = apply_edit(_config(strong=()), action="add", category="it_tech",
                           level="strong", keyword="switch")
    assert tilfoejet is not None and tilfoejet.categories[0].strong == ("switch",)


def test_apply_edit_afviser_ugyldige():
    assert apply_edit(_config(), action="flyt", category="it_tech",
                      level="strong", keyword="switch") is None
    assert apply_edit(_config(), action="remove", category="findes-ikke",
                      level="strong", keyword="switch") is None
    assert apply_edit(_config(), action="remove", category="it_tech",
                      level="niveau", keyword="switch") is None


def test_corpus_impact_viser_at_fjern_bryder_et_haardt_krav():
    impact = corpus_impact(_config(), CASES, action="remove", category="it_tech",
                           level="strong", keyword="switch")
    assert impact is not None
    assert "Switch TP-LINK TL-SG1016D" in impact["breaks"]


def test_corpus_impact_uden_facitliste_er_none():
    assert corpus_impact(_config(), [], action="remove", category="it_tech",
                         level="strong", keyword="switch") is None
