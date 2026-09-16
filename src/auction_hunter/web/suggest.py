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
from .queries import has_schema, keyword_noise

# Mindste antal lots et nøgleord skal have været med i, før det må foreslås
# fjernet. Under det er der ikke nok at dømme efter.
MIN_SEEN = 5
# Andelen af de lots der skal være afvist, før ordet er støjende nok.
MIN_SKIP_PCT = 60


@dataclass(frozen=True)
class Suggestion:
    """Et konkret, gennemprøveligt forslag til en ændring i profilen."""

    action: str      # kun 'remove' i dette lag
    keyword: str
    category: str
    level: str
    seen: int
    skip: int
    pct: int
    # Hårde facitliste-krav som ændringen ville bryde, hvis den er tjekket.
    breaks: tuple[str, ...] = ()
    # False når facitlisten ikke kunne læses her, så visningen kan se forskel på
    # "ingen effekt" og "kunne ikke tjekkes".
    checked: bool = False

    @property
    def reason(self) -> str:
        return f"{self.skip} af {self.seen} lots du fik, blev afvist"


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
