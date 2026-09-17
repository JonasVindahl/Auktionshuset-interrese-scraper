"""Hvad står der på lot-siden? Stand, skader og praktiske bemærkninger.

Lot-listen indeholder kun en titel, og en titel kan ikke sige om varen er i
stykker. Denne del henter lot-siden og leder efter de signaler der afgør om et
lot er værd at byde på.

Udtrækket tager lot'ets egen beskrivelse, ikke hele siden. Beskrivelsen er den
prosa-blok auktionshuset selv viser som varetekst. Auktionsbetingelserne ligger
ogsaa i en prosa-blok, men i en dropdown, og de ord de bruger ("reparation",
"afhentning", "momsfritagelse") siger intet om varen; scanner vi hele siden,
faar hvert eneste lot de samme flag. Mangler den kendte markup, falder vi
tilbage til den laengste sammenhaengende tekstblok.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

# Vendinger der ændrer hvad et lot er værd. Rækkefølgen er også den rækkefølge
# flagene rapporteres i, så det alvorligste står først.
SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defekt", ("defekt", "i stykker", "virker ikke", "fungerer ikke",
                "ufuldstændig", "mangler dele", "reparation")),
    ("reservedele", ("til reservedele", "reservedele", "reservedelslot",
                     "til reparation")),
    ("slidt", ("slidt", "brugsspor", "patina", "noget slidt")),
    ("komplet", ("komplet", "fuldt fungerende", "fuldt funktionsdygtig",
                 "alt tilbehør", "med alt tilbehør")),
    ("ny", ("ubrugt", "helt ny", "uåbnet", "i emballage", "fabriksny")),
    ("afhentning", ("afhentning", "afhentes", "hentes hos", "efter aftale")),
    ("momsfri", ("momsfri", "momsfrit", "uden moms")),
    ("donation", ("går til donation", "donation", "velgørenhed")),
)

# Det der ikke er selve indholdet.
_STRIP = ("script", "style", "noscript", "nav", "header", "footer",
          "aside", "form", "button", "svg", "iframe")

MIN_TEXT = 40

# Stand-signalerne oversat til noget der kan læses på en skærm og i en
# Discord-besked. Samme kilde begge steder, saa de ikke driver fra hinanden.
FLAG_LABELS = {
    "defekt": "Defekt",
    "reservedele": "Til reservedele",
    "slidt": "Slidt",
    "komplet": "Komplet",
    "ny": "Fremstår ny",
    "afhentning": "Afhentning",
    "momsfri": "Momsfri",
    "donation": "Donation",
}

# De flag der ændrer om et lot er værd at byde på. Resten er til at skimme.
SERIOUS_FLAGS = ("defekt", "reservedele")


@dataclass(frozen=True)
class Details:
    text: str = ""
    flags: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.text and not self.flags


def _clean(text: str) -> str:
    return " ".join(text.split())


def _longest(nodes: Iterable[Tag]) -> str:
    best = ""
    for node in nodes:
        text = _clean(node.get_text(" ", strip=True))
        if len(text) > len(best):
            best = text
    return best


def _text_of(html: str) -> str:
    """Brødteksten fra lot'ets egen beskrivelse."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(_STRIP):
        tag.decompose()

    # Auktionsbetingelserne ligger i en dropdown og er ikke lot'ets stand. De
    # ord de bruger ("reparation", "afhentning", "momsfritagelse"), giver
    # ellers de samme falske flag på hvert eneste lot.
    for tag in soup.select(".dropdown"):
        tag.decompose()

    # Lot'ets beskrivelse er den prosa-blok der ikke er en dropdown. Mangler
    # den, er der ingen beskrivelse, og så skal vi ikke gætte ud fra hele siden.
    beskrivelse = soup.select(".prosa")
    if beskrivelse:
        return _longest(beskrivelse)

    # Ukendt markup: den længste sammenhængende tekstblok er næsten altid
    # beskrivelsen. En menu er mange små stykker, en beskrivelse er ét.
    best = _longest(soup.find_all(["div", "section", "article", "main", "p", "body"]))
    if len(best) < MIN_TEXT:
        best = _clean(soup.get_text(" ", strip=True))
    return best


def find_signals(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    hits: list[str] = []
    for flag, phrases in SIGNALS:
        if any(phrase in lowered for phrase in phrases):
            hits.append(flag)
    return tuple(hits)


def parse(html: str, *, limit: int = 1200) -> Details:
    """Udtræk tekst og stand-signaler fra en lot-side."""
    if not html or not html.strip():
        return Details()
    text = _text_of(html)
    return Details(text=text[:limit], flags=find_signals(text))
