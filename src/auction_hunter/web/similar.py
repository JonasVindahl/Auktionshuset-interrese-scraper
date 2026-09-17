"""Lignende salg: hvad samme slags lot gik for, dengang det blev solgt.

Arkivet indeholder hvert lot agenten har set, også dem der aldrig ramte
interesseprofilen. Det er derfor den eneste kilde til hvad en vare faktisk går
for, og den står ubrugt hen hvis man kun ser på det lot man overvejer nu.

Metoden er bevidst enkel og forklarlig: titlen deles i betydningsbærende ord,
FTS5 finder kandidater der deler mindst ét ord, og Python kræver at der deles
mindst to — eller ét langt og sjældent nok til at være sig selv, som
"pladespiller" eller "synology". Det sidste er det der gør at et mærke alene
kan bære et match uden at hvert "div."-lot gør det samme.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..textmatch import normalize
from .formatting import parse_dt
from .queries import has_schema

# Ord der ikke siger noget om hvad varen er.
STOPWORDS = frozenset(["og", "i", "på", "til", "med", "for", "af", "om", "en", "et", "den", "det", "de", "som", "uden", "diverse", "div", "div.", "mv", "fl", "stk", "ca", "cirka", "inkl", "ekskl", "samt", "andre", "andet", "nye", "ny", "brugt", "ubrugt", "komplet"])

MIN_TERM_LENGTH = 3
# Modelnumre er det mest sigende i en auktionstitel: "650" og "600" er to
# forskellige varer, og "DS1817" og "DS920" er det. De består typisk af tal
# eller af bogstaver og tal blandet, og de ville falde ud af en naiv orddeling
# der kun beholdt ord på tre bogstaver eller mere.
MIN_NUMBER_LENGTH = 3
MAX_TERMS = 8
# Hvor mange kandidater FTS5 må give, før der scores. Nok til at de rigtige
# ligger i bunken, lille nok til at et sideskald forbliver hurtigt.
MAX_CANDIDATES = 80
# Et ord fra denne længde og op bærer et match alene.
STRONG_TERM_LENGTH = 6


@dataclass(frozen=True)
class Sale:
    """Et tidligere salg af noget der ligner."""

    lot_id: str
    title: str
    url: str
    hammer: int
    total: int
    ended_at: str
    shared: tuple[str, ...]
    weight: float = 0.0


@dataclass
class Comparables:
    items: list[Sale] = field(default_factory=list)
    median: int | None = None
    low: int | None = None
    high: int | None = None

    @property
    def count(self) -> int:
        return len(self.items)


def _keep(word: str) -> bool:
    if word in STOPWORDS:
        return False
    has_digit = any(c.isdigit() for c in word)
    has_letter = any(c.isalpha() for c in word)
    if has_digit and has_letter:
        return True                       # ds1817, 87v, ax88u
    if word.isdigit():
        return len(word) >= MIN_NUMBER_LENGTH   # 650, 1200 — men ikke 8
    return len(word) >= MIN_TERM_LENGTH


def terms(title: str) -> list[str]:
    """Betydningsbærende ord fra en titel, i den rækkefølge de står."""
    words = re.findall(r"[a-zæøå0-9]+", normalize(title))
    out: list[str] = []
    for word in words:
        if not _keep(word) or word in out:
            continue
        out.append(word)
    return out[:MAX_TERMS]


def _idf(conn: sqlite3.Connection, term: str) -> float:
    """Hvor sjældent et ord er i arkivet. Sjældne ord vejer tungere.

    Uden dette rangordnes "Sennheiser HD 650" og "Sennheiser HD 600" ens, fordi
    begge deler mærke og produkttype. Modelnummeret er det der skiller dem, og
    det er netop sjældent.
    """
    import math

    try:
        total = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
        hits = conn.execute(
            "SELECT COUNT(*) FROM lots_fts WHERE lots_fts MATCH ?", (f'"{term}"',)
        ).fetchone()[0]
    except sqlite3.Error:
        return 1.0
    if not hits:
        return 0.0
    return math.log(1 + total / hits)


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2)


def find(
    conn: sqlite3.Connection,
    *,
    lot_id: str,
    title: str,
    limit: int = 6,
) -> Comparables:
    """Find tidligere salg af samme slags. Kun afsluttede lots har en pris."""
    if not has_schema(conn, "lots", "notifications"):
        return Comparables()

    words = terms(title)
    # Et enkelt ord giver for mange kandidater til at være et match.
    if len(words) < 2:
        return Comparables()

    weights = {word: _idf(conn, word) for word in words}
    match = " OR ".join(f'"{word}"*' for word in words)
    rows = conn.execute(
        """
        SELECT l.lot_id, l.title, l.url, l.last_bid, l.last_total, l.ends_at
        FROM lots l
        JOIN lots_fts fts ON fts.lot_id = l.lot_id
        WHERE lots_fts MATCH ?
        LIMIT ?
        """,
        (match, MAX_CANDIDATES),
    ).fetchall()

    now = datetime.now(UTC)
    sales: list[Sale] = []
    for row in rows:
        if row["lot_id"] == lot_id:
            continue
        ends = parse_dt(row["ends_at"])
        # Kun afsluttede lots: et aktivt lot har ingen slutpris endnu.
        if ends is None or ends > now:
            continue
        hammer = row["last_bid"] or 0
        if hammer <= 0:
            continue
        total = row["last_total"] or hammer
        normalized = normalize(row["title"] or "")
        shared = tuple(w for w in words if w in normalized)
        if len(shared) < 2 and not any(
            len(w) >= STRONG_TERM_LENGTH for w in shared
        ):
            continue
        sales.append(Sale(
            lot_id=row["lot_id"], title=row["title"] or "", url=row["url"] or "",
            hammer=int(hammer), total=int(total),
            ended_at=ends.isoformat(timespec="seconds"), shared=shared,
            weight=sum(weights.get(w, 1.0) for w in shared),
        ))

    # Størst fælles vægt først — altså de nærmeste salg — og derefter senest.
    sales.sort(key=lambda s: s.ended_at, reverse=True)
    sales.sort(key=lambda s: s.weight, reverse=True)
    top = sales[:limit]
    # Medianen regnes over dem der vises, saa tallet passer til listen.
    totals = [s.total for s in top]
    return Comparables(
        items=top,
        median=_median(totals),
        low=min(totals) if totals else None,
        high=max(totals) if totals else None,
    )
