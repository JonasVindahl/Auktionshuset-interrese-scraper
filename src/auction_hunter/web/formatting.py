"""Formatering af tid og priser til visning.

Tidsberegninger ligger her og ikke i SQL. ``ends_at`` gemmes som ISO med
tidszone-offset (``2026-09-16T14:30:00+02:00``), mens SQLites
``datetime('now')`` giver ``2026-09-16 12:05:24``. En strengsammenligning
mellem de to er forkert, fordi ``T`` sorterer efter mellemrum — så alt ville
se ud til at ligge i fremtiden.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Under så mange timer tilbage markeres et lot som "haster".
URGENT_HOURS = 6
SOON_HOURS = 24


def parse_dt(value: str | None) -> datetime | None:
    """ISO-streng til aware datetime. Naive strenge antages at være UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


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
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    full = dt.astimezone().strftime("%d/%m %H:%M")
    if secs < 120:
        return "lige nu", full
    if secs < 3600:
        return f"{secs // 60} min siden", full
    if secs < 86400:
        return f"{secs // 3600} t siden", full
    return f"{secs // 86400} dage siden", full


def time_left(ends_at: str | None) -> tuple[str, str, int]:
    """(tekst, css-klasse, sekunder tilbage).

    Sekunder er 0 når lot'et er slut eller uden sluttidspunkt, så frontenden kan
    sortere de aktive først uden at kende formatet.
    """
    dt = parse_dt(ends_at)
    if dt is None:
        return "", "", 0

    secs = int((dt - datetime.now(timezone.utc)).total_seconds())

    if secs <= 0:
        past = abs(secs)
        if past < 3600:
            return f"Sluttede {past // 60} min siden", "ended", 0
        if past < 86400:
            return f"Sluttede {past // 3600} t siden", "ended", 0
        return f"Sluttede {past // 86400} dage siden", "ended", 0

    # Timer og minutter under et døgn. Ren timevisning trunkerer 2t59m til
    # "2 t", hvilket ser ud som om der er en time mindre tilbage end der er.
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
    day = dt.date()
    today = datetime.now(timezone.utc).date()
    delta = (today - day).days
    if delta == 0:
        return "I dag"
    if delta == 1:
        return "I går"
    if delta < 7:
        return f"{delta} dage siden"
    return day.strftime("%-d. %B %Y") if delta > 300 else day.strftime("%-d. %B")
