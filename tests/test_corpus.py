"""Facitliste-test: matcher interesseprofilen de rigtige varer?

I modsætning til de øvrige tests bruger denne rigtige lot-titler fra
auktionshuset.dk. Titlerne ændrer sig over tid, men denne fil gør det ikke —
den er den versionerede beslutning om hvad der er interessant.

Derfor er det trygt at tune interests.yml: ændringer valideres mod et fast
facit i stedet for mod et tilfældigt øjebliksbillede af auktionerne.

'yes' og 'no' er hårde krav. 'maybe' er grænsetilfælde hvor begge svar er
forsvarlige, og de tælles kun som oplysning.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from auction_hunter.config import load_config
from auction_hunter.matcher import match_lot
from auction_hunter.scraper import Lot

# Facitlisten bor i config/ sammen med interesseprofilen, saa den ogsaa er med
# i Docker-imaget. Dashboardet bruger samme fil til at vurdere et forslag foer
# det anvendes.
CORPUS = Path(__file__).resolve().parent.parent / "config" / "match_expectations.jsonl"


def load_corpus() -> list[dict]:
    lines = CORPUS.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


CASES = load_corpus()


@pytest.fixture(scope="module")
def config():
    return load_config("config/interests.yml")


def make_lot(title: str) -> Lot:
    return Lot(
        lot_id="corpus", title=title,
        url="https://auktionshuset.dk/auktioner/test/lots/1/x",
        lot_number="1", auction_id="A1", auction_title="Testauktion",
        current_bid=100, total_price=188, ends_at=None, image_url="",
        has_bids=True,
    )


def matched_categories(title: str, config) -> set[str]:
    return {m.category.key for m in match_lot(make_lot(title), config)}


def matched_keywords(title: str, config) -> set[str]:
    return {
        keyword.strip().lower()
        for m in match_lot(make_lot(title), config)
        for keyword in m.keywords
    }


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["expect"] == "yes"], ids=lambda c: c["title"][:44]
)
def test_interessante_varer_matches(case, config):
    assert matched_categories(case["title"], config), (
        f"BURDE MATCHE: {case['title']!r} — {case['why']}"
    )


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.get("via")], ids=lambda c: c["title"][:44]
)
def test_via_noegleordet_baerer_casen(case, config):
    """En case med 'via' skal rammes af netop det nøgleord.

    Uden dette er en case opfyldt så snart noget rammer, og den kan derfor gå
    igennem ad en anden vej end den den skulle dække. Facitlistens seks
    netværkslinjer så ud som forleds-tilfælde, men blev alle båret af
    efterleddet 'switch'.
    """
    hits = matched_keywords(case["title"], config)
    assert case["via"].lower() in hits, (
        f"{case['title']!r} skulle baeres af {case['via']!r}, men ramte {sorted(hits)}"
    )


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["expect"] == "no"], ids=lambda c: c["title"][:44]
)
def test_uinteressante_varer_matches_ikke(case, config):
    cats = matched_categories(case["title"], config)
    assert not cats, (
        f"BURDE IKKE MATCHE: {case['title']!r} — {case['why']} (fangede {cats})"
    )


def test_facitlisten_daekker_alle_udfald(config):
    """Sanity: facitlisten skal have alle tre typer, ellers tester den intet."""
    kinds = Counter(c["expect"] for c in CASES)
    assert kinds["yes"] >= 5
    assert kinds["no"] >= 5
    assert kinds["maybe"] >= 1


def test_maybe_sager_rapporteres(config):
    """Grænsetilfælde må ikke fejle testen, men skal kunne ses i output."""
    interesting = []
    for case in CASES:
        if case["expect"] == "maybe" and matched_categories(case["title"], config):
            interesting.append(case["title"])
    if interesting:
        print(f"\n{len(interesting)} af {sum(1 for c in CASES if c['expect'] == 'maybe')} "
              f"grænsetilfælde matcher og sendes til Discord:")
        for title in interesting:
            print(f"  - {title}")
