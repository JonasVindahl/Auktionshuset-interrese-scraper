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
from ..details import FLAG_LABELS, SERIOUS_FLAGS
from ..fees import DEFAULT_OPENING_BID
from ..fees import estimate as price_estimate
from .formatting import (
    LOCAL_TZ,
    date_label,
    kr,
    parse_dt,
    rel_past,
    time_left,
    timestamp,
)


def _get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def prepare(
    row: sqlite3.Row,
    *,
    seen_field: str = "sent_at",
    db_path: str | None = None,
    opening_bid: int = DEFAULT_OPENING_BID,
) -> dict[str, Any]:
    """Én række klar til skabelonen.

    ``db_path`` bruges til at slå op om der findes et lokalt billede. Er der
    et, peges der på det i stedet for auktionshusets adresse — den er død så
    snart lot'et er afsluttet.
    """
    first_bid = _get(row, "first_bid")
    last_bid = _get(row, "last_bid")
    # Prisen er den reelle pris inkl. salær og moms. Har lot'et ingen bud, er
    # 0 kr ikke meningsfuldt: saa vises hvad det koster at vaere den foerste.
    # Det er samme model som agenten bruger, saa tallene ikke kan divergere.
    price = price_estimate(
        last_bid,
        auction_title=_get(row, "auction_title") or "",
        opening_bid=opening_bid,
    )
    if price.current_total is not None:
        # Den aktuelle total vinder over den pris der blev sendt ved notifikation.
        cost = _get(row, "last_total") or _get(row, "cost") or price.current_total
        price_text = kr(cost)
        price_label = ""
    else:
        cost = price.entry_cost
        price_text = kr(cost)
        price_label = "åbner ved"
    ends_at = _get(row, "ends_at")
    seen = _get(row, seen_field) or _get(row, "first_seen")

    left, css, ends_in = time_left(ends_at)
    seen_rel, seen_full = rel_past(seen)

    # Det præcise tidspunkt ved siden af den relative tid: den ene siger om man
    # skal handle nu, den anden hvornår man skal sidde klar.
    flags = tuple(
        flag for flag in str(_get(row, "details_flags") or "").split(",") if flag
    )

    ends_dt = parse_dt(ends_at)
    ends_text = (ends_dt.astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")
                 if ends_dt else "")

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
        "profile_key": _get(row, "profile_key") or "standard",
        "feedback": _get(row, "feedback_action") or "",
        "image": image,
        "cost": cost,
        "price": price_text,
        "price_label": price_label,
        "flags": flags,
        "details": _get(row, "details") or "",
        "flags_label": ", ".join(FLAG_LABELS.get(f, f) for f in flags),
        "has_serious_flag": any(f in SERIOUS_FLAGS for f in flags),
        "bid": last_bid or 0,
        "bid_text": kr(last_bid) if last_bid else "–",
        "ends_at_text": ends_text,
        "rise": rise,
        "is_estimate": price.is_estimate,
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
    opening_bid: int = DEFAULT_OPENING_BID,
) -> list[dict]:
    return [
        prepare(row, seen_field=seen_field, db_path=db_path, opening_bid=opening_bid)
        for row in rows
    ]


def group_by_date(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Bevar rækkefølgen, men saml kort under deres dato-overskrift."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    return list(groups.items())
