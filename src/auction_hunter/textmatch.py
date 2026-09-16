"""Tekstnormalisering og nøgleordsmatchning med ordgrænser.

Naiv substring-søgning giver alvorlig støj på dansk: 'ur' matcher 'Taburetter',
'sdr' matcher 'Overtræksdragter', 'rel' matcher 'varelager'. Derfor normaliseres
teksten og matches med ordgrænser i stedet.

Dansk er samtidig et sammensat sprog — 'netværksswitch', 'armbåndsur',
'espressomaskine' — så et nøgleord skal kunne stå i *starten* eller *enden* af
et ord, ikke kun som selvstændigt ord. Et nøgleord begravet i midten af et ord
afvises dog, og korte nøgleord (< RELAXED_MIN_LENGTH) kræver eksakt match, så
'ur' ikke udløses af 'natur' eller 'kultur'.

Normaliseringen folder æøå og accenter, så 'højttaler' og 'hojtaler' behandles
ens. Tal betragtes ikke som bogstaver, så 'proxmark' matcher 'Proxmark3'.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Ordbøjning i dansk tilføjer endelser ('forstærker', 'switches'), så korte ord
# er for risikable at matche løst. Længere ord tåler derfor at matche som led i
# sammensatte ord og med bøjningsendelser.
RELAXED_MIN_LENGTH = 5

# Bøjningsendelser der må følge efter et nøgleord, så 'switch' også fanger
# 'switches' og 'forstærker' fanger 'forstærkeren'.
_INFLECTIONS = r"(?:erne|ende|ens|ets|er|en|et|es|e|s)?"

_FOLD = str.maketrans(
    {
        "æ": "ae", "ø": "oe", "å": "aa", "ä": "ae", "ö": "oe", "ü": "ue",
        "é": "e", "è": "e", "ê": "e", "á": "a", "à": "a", "ó": "o", "ú": "u",
        "ß": "ss", "&": " og ",
    }
)


def normalize(text: str) -> str:
    """Lowercase, fold danske tegn og reducér tegnsætning til mellemrum."""
    lowered = text.lower().translate(_FOLD)
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


# Den anden måde folk folder æøå på: ét bogstav i stedet for to. Begge dele er
# almindelige når man skriver uden danske taster, så søgningen skal kunne
# genkende både 'hoejttaler' og 'hojttaler'.
_FOLD_LOOSE = str.maketrans(
    {
        "æ": "a", "ø": "o", "å": "a", "ä": "a", "ö": "o", "ü": "u",
        "é": "e", "è": "e", "ê": "e", "á": "a", "à": "a", "ó": "o", "ú": "u",
        "ß": "ss", "&": " og ",
    }
)


def normalize_loose(text: str) -> str:
    """Som ``normalize``, men æøå bliver til ét bogstav i stedet for to.

    Bruges kun til fritekstsøgning, aldrig til nøgleordsmatchning: den er for
    upræcis til at afgøre om et lot er interessant, men præcis nok til at finde
    noget man leder efter.
    """
    lowered = text.lower().translate(_FOLD_LOOSE)
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


@lru_cache(maxsize=8192)
def keyword_pattern(keyword: str) -> re.Pattern[str] | None:
    """Byg et regex for et normaliseret nøgleord.

    Lange nøgleord må optræde som selvstændigt ord, som forled eller som
    efterled i et sammensat ord, og må bøjes. Korte nøgleord kræver et helt ord,
    så 'ur' ikke udløses af 'natur' eller 'euro'. Returnerer None hvis
    nøgleordet er tomt efter normalisering.
    """
    normalized = normalize(keyword)
    if not normalized:
        return None
    phrase = re.escape(normalized).replace(r"\ ", r"\s+")

    # Selvstændigt ord. Tillader efterfølgende tal, så 'proxmark3' matcher.
    as_word = rf"(?<![a-z0-9]){phrase}(?![a-z])"

    if len(normalized) < RELAXED_MIN_LENGTH:
        return re.compile(as_word)

    # Selvstændigt ord med bøjning ('switches'), eller som led i et sammensat
    # dansk ord ('netværksswitch', 'armbåndsur').
    standalone = rf"(?<![a-z0-9]){phrase}{_INFLECTIONS}(?![a-z])"
    compound = rf"{phrase}{_INFLECTIONS}(?![a-z])"
    return re.compile(rf"(?:{standalone}|{compound})")


@lru_cache(maxsize=8192)
def exact_pattern(keyword: str) -> re.Pattern[str] | None:
    """Regex der KUN matcher nøgleordet som selvstændigt ord."""
    normalized = normalize(keyword)
    if not normalized:
        return None
    phrase = re.escape(normalized).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){phrase}(?![a-z])")


def find_keywords(
    text: str, keywords: tuple[str, ...], *, exact: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Returnér de nøgleord fra listen der optræder i teksten.

    ``exact`` er nøgleord der kun må matche som selvstændigt ord. Det bruges til
    mærkenavne der er sammenfaldende med dele af andre ord — 'mission' er et
    højttalermærke, men står også i 'transmission'.
    """
    if not text or not keywords:
        return ()
    normalized = normalize(text)
    exact_forms = {normalize(kw) for kw in exact}
    hits: list[str] = []
    for keyword in keywords:
        if normalize(keyword) in exact_forms:
            pattern = exact_pattern(keyword)
        else:
            pattern = keyword_pattern(keyword)
        if pattern is not None and pattern.search(normalized):
            hits.append(keyword)
    return tuple(hits)


# Endelser der fjernes når to nøgleord skal afgøres som samme ord. Listen er
# ordnet længste-først, så 'højtalere' og 'højtaler' bliver samme rod.
_ENDINGS = ("erne", "ende", "ens", "ets", "ere", "er", "en", "et", "es", "e", "s")


def _stem(normalized: str) -> str:
    for ending in _ENDINGS:
        if normalized.endswith(ending) and len(normalized) - len(ending) >= 3:
            return normalized[: -len(ending)]
    return normalized


def distinct_forms(keywords: tuple[str, ...]) -> set[str]:
    """Reducér nøgleord til deres unikke rødder.

    'strømforsyning' og 'stroemforsyning' samt 'højtaler' og 'højtalere' er
    samme signal og skal kun tælle én gang, når et match vurderes.
    """
    return {_stem(normalize(kw)) for kw in keywords if normalize(kw)}
