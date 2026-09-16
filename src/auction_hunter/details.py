"""Hvad står der på lot-siden? Stand, skader og praktiske bemærkninger.

Lot-listen indeholder kun en titel, og en titel kan ikke sige om varen er i
stykker. Denne del henter lot-siden og leder efter de signaler der afgør om et
lot er værd at byde på.

Udtrækket er bevidst strukturuafhængigt. Vi kender ikke auktionshusets markup,
og en CSS-selektor der gættes forkert fejler tavst den dag siden ændrer sig. I
stedet tages sidens brødtekst — uden navigation, scripts og footere — og der
ledes efter danske vendinger. Det giver et grovere men ærligt svar, og listen
kan udvides når nogen har set den rigtige side.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bs4 import BeautifulSoup

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


def _text_of(html: str) -> str:
    """Brødteksten fra sidens største indholdsblok."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(_STRIP):
        tag.decompose()

    # Den længste sammenhængende tekstblok er næsten altid beskrivelsen. En
    # menu er mange små stykker, en beskrivelse er ét.
    best = ""
    for node in soup.find_all(["div", "section", "article", "main", "p", "body"]):
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) > len(best):
            best = text
    if len(best) < MIN_TEXT:
        best = " ".join(soup.get_text(" ", strip=True).split())
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
