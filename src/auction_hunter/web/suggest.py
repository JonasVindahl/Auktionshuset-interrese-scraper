"""Forslag til hvordan interesseprofilen kan blive bedre, uden en model.

Feedbacken er de eneste mærkater vi har: hvert "afvist" er en falsk positiv, og
hvert "budt" eller "købt" er et fund der ramte rigtigt. Denne del finder
mønstrene deterministisk, med et minimum antal observationer bag hvert forslag,
så et enkelt underligt lot ikke fører til en ændring. AI-laget lægges senere
ovenpå og formulerer og prioriterer; det foreslår aldrig selv nøgleord.

Intet anvendes automatisk. Et forslag er et forslag, og profilen tunes mod
facitlisten, ikke mod et øjebliksbillede.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Any

from .. import evaluate
from ..textmatch import normalize
from .queries import has_schema, keyword_noise

# Mindste antal lots et nøgleord skal have været med i, før det må foreslås
# fjernet. Under det er der ikke nok at dømme efter.
MIN_SEEN = 5
# Andelen af de lots der skal være afvist, før ordet er støjende nok.
MIN_SKIP_PCT = 60


@dataclass(frozen=True)
class Suggestion:
    """Et konkret, gennemprøveligt forslag til en ændring i profilen."""

    action: str      # 'remove' eller 'add'
    keyword: str
    category: str
    level: str
    seen: int = 0
    skip: int = 0
    pct: int = 0
    reason: str = ""
    # Hårde facitliste-krav som ændringen ville bryde, hvis den er tjekket.
    breaks: tuple[str, ...] = ()
    # False når facitlisten ikke kunne læses her, så visningen kan se forskel på
    # "ingen effekt" og "kunne ikke tjekkes".
    checked: bool = False
    # 'regel' for den regelbaserede analyse, 'model' for modellens forslag.
    source: str = "regel"


def _level_of(category: Any, keyword: str) -> str:
    """Hvilket niveau ordet står på, så fjern-knappen rammer det rigtige."""
    for level in ("strong", "weak", "brands", "exact"):
        if keyword in getattr(category, level, ()):
            return level
    return "weak"


def noisy_keywords(
    conn: sqlite3.Connection,
    config: Any,
    *,
    min_seen: int = MIN_SEEN,
    min_pct: int = MIN_SKIP_PCT,
    limit: int = 8,
) -> list[Suggestion]:
    """Nøgleord der oftest fører til noget du afviser."""
    out: list[Suggestion] = []
    for row in keyword_noise(conn, config, min_seen=min_seen, limit=limit * 2):
        if row["pct"] < min_pct:
            continue
        category = config.category(row["category"])
        if category is None:
            continue
        out.append(
            Suggestion(
                action="remove",
                keyword=row["keyword"],
                category=row["category"],
                level=_level_of(category, row["keyword"]),
                seen=row["seen"],
                skip=row["skip"],
                pct=row["pct"],
                reason=f"{row['skip']} af {row['seen']} lots du fik, blev afvist",
            )
        )
        if len(out) >= limit:
            break
    return out


LEVELS = ("strong", "weak", "brands", "exact")


def from_model(
    entries: list[dict], config: Any, titles: list[str], *, limit: int = 6
) -> list[Suggestion]:
    """Validér modellens forslag mod den faktiske konfiguration.

    Modellen må ikke kunne indføre et nøgleord der ikke står i en titel, en
    kategori der ikke findes, eller et niveau der ikke findes. Alt andet er et
    forslag vi ikke kan efterprøve, og så er det ikke et forslag.
    """
    if not entries or not titles:
        return []

    known = {cat.key: cat for cat in config.categories}
    haystack = " \n ".join(normalize(title) for title in titles)
    out: list[Suggestion] = []
    seen: set[tuple[str, str, str]] = set()

    for entry in entries:
        keyword = str(entry.get("noegleord") or entry.get("nøgleord") or "").strip().lower()
        category = str(entry.get("kategori") or "").strip()
        level = str(entry.get("niveau") or "").strip().lower()
        if not keyword or category not in known or level not in LEVELS:
            continue
        if normalize(keyword) not in haystack:
            continue
        if keyword in getattr(known[category], level):
            continue
        identity = (category, level, keyword)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(
            Suggestion(
                action="add",
                keyword=keyword,
                category=category,
                level=level,
                reason=str(entry.get("grund") or "modellens forslag").strip()[:200],
                source="model",
            )
        )
        if len(out) >= limit:
            break
    return out


def annotate_impact(
    suggestions: list[Suggestion], config: Any, cases: list[dict]
) -> list[Suggestion]:
    """Sæt facitlistens svar på hvert forslag, før det vises.

    Et forslag der bryder et hårdt krav er ikke nødvendigvis forkert, men det
    skal stå klart at det er en afvejning, ikke en gratis forbedring.
    """
    if not cases:
        return suggestions
    out: list[Suggestion] = []
    for suggestion in suggestions:
        impact = evaluate.corpus_impact(
            config, cases,
            action=suggestion.action, category=suggestion.category,
            level=suggestion.level, keyword=suggestion.keyword,
        )
        out.append(replace(
            suggestion,
            breaks=tuple(impact["breaks"]) if impact else (),
            checked=impact is not None,
        ))
    return out


def unmatched_marks(conn: sqlite3.Connection, config: Any = None, *, limit: int = 12) -> list[dict]:
    """Lots du har budt på eller købt, som profilen ikke fangede.

    Et tegn på at der mangler et nøgleord. Ordet foreslås ikke her: det kræver
    at man forstår varen, og det er AI-laget.
    """
    if not has_schema(conn, "feedback", "lots", "notifications"):
        return []
    rows = conn.execute(
        """
        SELECT l.lot_id, l.title, f.action
        FROM feedback f
        JOIN lots l ON l.lot_id = f.lot_id
        WHERE f.action IN ('bid', 'bought')
          AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.lot_id = f.lot_id)
        ORDER BY f.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
