"""Forbered databaserækker til visning.

``sqlite3.Row`` er upraktisk i skabeloner: den har ingen ``.get()``, ingen
attributadgang, og et manglende felt giver en fejl frem for en tom værdi.
Rækkerne oversættes derfor til almindelige dicts med de afledte værdier
(pris som tekst, tid tilbage, relativ tid) beregnet på forhånd — så
skabelonen kun skal vise tal, ikke regne dem ud.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote

from .. import images
from .formatting import date_label, kr, rel_past, time_left, timestamp


def _get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def prepare(
    row: sqlite3.Row,
    *,
    seen_field: str = "sent_at",
    db_path: str | None = None,
) -> dict[str, Any]:
    """Én række klar til skabelonen.

    ``db_path`` bruges til at slå op om der findes et lokalt billede. Er der
    et, peges der på det i stedet for auktionshusets adresse — den er død så
    snart lot'et er afsluttet.
    """
    cost = _get(row, "cost") or _get(row, "last_total") or _get(row, "last_bid") or 0
    first_bid = _get(row, "first_bid")
    last_bid = _get(row, "last_bid")
    ends_at = _get(row, "ends_at")
    seen = _get(row, seen_field) or _get(row, "first_seen")

    left, css, ends_in = time_left(ends_at)
    seen_rel, seen_full = rel_past(seen)

    lot_id = _get(row, "lot_id", "")
    image = _get(row, "image_url") or ""
    if not str(image).startswith("http"):
        image = ""
    # Et cachet billede vinder: originalen forsvinder når auktionen lukker.
    if db_path and lot_id and images.is_cached(db_path, lot_id):
        image = f"/image/{quote(str(lot_id), safe='')}"

    rise = ""
    if first_bid is not None and last_bid is not None and last_bid > first_bid:
        rise = kr(last_bid - first_bid)

    return {
        "lot_id": lot_id,
        "title": _get(row, "title") or f"Lot {lot_id}",
        "url": _get(row, "url") or "",
        "auction": _get(row, "auction_title") or "",
        "lot_number": _get(row, "lot_number") or "",
        "category": _get(row, "category_key") or "",
        "feedback": _get(row, "feedback_action") or "",
        "image": image,
        "cost": cost,
        "price": kr(cost),
        "rise": rise,
        "is_estimate": not last_bid,
        "ts": timestamp(seen),
        "seen_rel": seen_rel,
        "seen_full": seen_full,
        "time_left": left,
        "time_css": css,
        "ends_in": ends_in,
        "was_match": bool(_get(row, "was_match", 1)),
        "group": date_label(seen),
    }


def prepare_all(
    rows: list[sqlite3.Row],
    *,
    seen_field: str = "sent_at",
    db_path: str | None = None,
) -> list[dict]:
    return [prepare(row, seen_field=seen_field, db_path=db_path) for row in rows]


def group_by_date(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Bevar rækkefølgen, men saml kort under deres dato-overskrift."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    return list(groups.items())
