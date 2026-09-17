"""Formatering af tid og priser til visning.

Deles af agenten (Discord) og webdashboardet, saa et beloeb eller en tid ikke
kan formateres forskelligt to steder.

Tidsberegninger ligger her og ikke i SQL. ``ends_at`` gemmes som ISO med
tidszone-offset (``2026-09-16T14:30:00+02:00``), mens SQLites
``datetime('now')`` giver ``2026-09-16 12:05:24``. En strengsammenligning
mellem de to er forkert, fordi ``T`` sorterer efter mellemrum — så alt ville
se ud til at ligge i fremtiden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Containeren kører UTC, men brugeren sidder i Danmark. Alt der vises som et
# klokkeslæt eller grupperes pr. dag skal derfor regnes i dansk tid, ellers
# stemmer "I dag" og "set 10 min siden" ikke med det brugeren ser på uret.
try:
    LOCAL_TZ: Any = ZoneInfo("Europe/Copenhagen")
except ZoneInfoNotFoundError:      # pragma: no cover - kun hvis tzdata mangler
    LOCAL_TZ = UTC

# Under så mange timer tilbage markeres et lot som "haster".
URGENT_HOURS = 6
SOON_HOURS = 24

# Danske månedsnavne. strftime("%B") følger systemets locale, og både containeren
# og dette miljø kører med C-locale, hvor måneden hedder "February". Derfor listen
# her i stedet, så datoer ældre end en uge ikke skifter sprog under brugeren.
MONTHS_DA = (
    "januar", "februar", "marts", "april", "maj", "juni",
    "juli", "august", "september", "oktober", "november", "december",
)


def _danish_date(day) -> str:
    return f"{day.day}. {MONTHS_DA[day.month - 1]}"


def parse_dt(value: str | None) -> datetime | None:
    """ISO-streng til aware datetime. Naive strenge antages at være UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def timestamp(value: str | None) -> int:
    dt = parse_dt(value)
    return int(dt.timestamp()) if dt else 0


def kr(value: int | None) -> str:
    """1234 -> '1.234 kr'. Dansk tusindtalsseparator."""
    if not value:
        return "–"
    return f"{value:,} kr".replace(",", ".")


def rel_past(value: str | None) -> tuple[str, str]:
    """(relativ tekst, fuldt tidspunkt) for noget der er sket."""
    dt = parse_dt(value)
    if dt is None:
        return "", ""
    secs = int((datetime.now(UTC) - dt).total_seconds())
    full = dt.astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")
    if secs < 120:
        return "lige nu", full
    if secs < 3600:
        return f"for {secs // 60} min. siden", full
    if secs < 86400:
        return f"for {secs // 3600} t. siden", full
    days = secs // 86400
    return (f"for {days} dag siden" if days == 1 else f"for {days} dage siden"), full


def time_left(ends_at: str | None) -> tuple[str, str, int]:
    """(tekst, css-klasse, sekunder tilbage).

    Sekunder er 0 når lot'et er slut eller uden sluttidspunkt, så frontenden kan
    sortere de aktive først uden at kende formatet.
    """
    dt = parse_dt(ends_at)
    if dt is None:
        # Uden en sluttid er der ingen nedtælling. At vise ingenting skjuler at
        # data mangler, så det siges højt i samme stil som de øvrige tider.
        return "Sluttid ukendt", "unknown", 0

    secs = int((dt - datetime.now(UTC)).total_seconds())

    if secs <= 0:
        past = abs(secs)
        if past < 3600:
            return f"Sluttede {past // 60} min siden", "ended", 0
        if past < 86400:
            return f"Sluttede {past // 3600} t siden", "ended", 0
        days = past // 86400
        return (
            f"Sluttede {days} dag siden" if days == 1
            else f"Sluttede {days} dage siden"
        ), "ended", 0

    # Timer og minutter under et døgn. Ren timevisning trunkerer 2t59m til
    # "2 t", hvilket ser ud som om der er en time mindre tilbage end der er.
    if secs < 60:
        return "Slutter om under et minut", "urgent", secs
    if secs < 3600:
        return f"Slutter om {secs // 60} min", "urgent", secs
    if secs < SOON_HOURS * 3600:
        hours, minutes = secs // 3600, (secs % 3600) // 60
        text = f"Slutter om {hours} t" + (f" {minutes} min" if minutes else "")
        return text, ("urgent" if secs < URGENT_HOURS * 3600 else "soon"), secs
    days, hours = secs // 86400, (secs % 86400) // 3600
    text = f"Slutter om {days} dag" + ("e" if days != 1 else "")
    if hours:
        text += f" {hours} t"
    return text, "", secs


def date_label(value: str | None) -> str:
    """Gruppeoverskrift: 'I dag', 'I går', '3 dage siden', '12. september'."""
    dt = parse_dt(value)
    if dt is None:
        return "Ukendt"
    day = dt.astimezone(LOCAL_TZ).date()
    today = datetime.now(LOCAL_TZ).date()
    delta = (today - day).days
    if delta < 0:
        # En dato i fremtiden må ikke blive "-1 dage siden".
        return "Senere"
    if delta == 0:
        return "I dag"
    if delta == 1:
        return "I går"
    if delta < 7:
        return f"{delta} dage siden"
    return _danish_date(day) + (f" {day.year}" if delta > 300 else "")