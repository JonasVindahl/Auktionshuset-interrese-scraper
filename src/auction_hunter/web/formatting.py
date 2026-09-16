"""Dansk formatering til webdashboardet.

Selve formateringen ligger i auction_hunter.formatting, fordi agenten bruger
praecis de samme funktioner i Discord-embeds. Her re-eksporteres de, saa
web-laget kan importere dem som foer.
"""

from __future__ import annotations

from ..formatting import (  # noqa: F401
    LOCAL_TZ,
    MONTHS_DA,
    SOON_HOURS,
    URGENT_HOURS,
    date_label,
    kr,
    parse_dt,
    rel_past,
    time_left,
    timestamp,
)

__all__ = [
    "LOCAL_TZ", "MONTHS_DA", "SOON_HOURS", "URGENT_HOURS",
    "date_label", "kr", "parse_dt", "rel_past", "time_left", "timestamp",
]
