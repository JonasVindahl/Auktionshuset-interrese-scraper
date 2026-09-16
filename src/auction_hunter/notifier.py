"""Discord-notifikationer via webhook.

Discord har et par grænser der er værd at respektere:

* Maks. 10 embeds pr. besked
* Maks. 2000 tegn i indhold, 4096 i en embed-beskrivelse
* Rate limit pr. webhook (429 med ``retry_after`` i JSON)

Fund grupperes derfor pr. kategori, og der sendes én besked ad gangen med
respekt for ``retry_after``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

from .details import FLAG_LABELS
from .formatting import kr, time_left
from .matcher import Match, last_chance

log = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 10
COLOR_GOOD = 0x2ECC71
COLOR_OVER_BUDGET = 0xE67E22
COLOR_LAST_CHANCE = 0xE74C3C


class DiscordError(RuntimeError):
    """Kunne ikke levere beskeden til Discord."""


@dataclass
class NotifyResult:
    sent: list[Match] = field(default_factory=list)
    sent_messages: int = 0
    failed: list[Match] = field(default_factory=list)

    @property
    def sent_embeds(self) -> int:
        return len(self.sent)


@dataclass(frozen=True)
class PriceAlert:
    """Et lot brugeren foelger eller har budt paa, hvor prisen er steget."""

    lot_id: str
    title: str
    url: str
    old_cost: int | None
    new_cost: int
    ends_at: str | None = None


def _format_price(match: Match) -> str:
    """Prisfelt med tydelig markering af estimerede beløb.

    Formateringen deles med webdashboardet, saa et beløb ikke kan skrives på
    to måder alt efter hvor man læser det.
    """
    price = match.price
    if price.current_total is not None:
        base = f"**{kr(price.current_total)}**"
        base += f"\n(aktuelt bud {kr(price.current_bid)} inkl. moms & salær)"
    else:
        base = f"**~{kr(price.entry_cost)}**"
        base += "\n(estimat: første bud inkl. moms & salær)"
    if price.vat_exempt:
        base += "\nMomsfri auktion"
    return base


def build_embed(
    match: Match, *, last_chance_hours: float = 24, flags: tuple[str, ...] = ()
) -> dict:
    """Byg én Discord-embed for et fund."""
    lot = match.lot

    if last_chance(lot, last_chance_hours):
        color = COLOR_LAST_CHANCE
    elif match.over_budget:
        color = COLOR_OVER_BUDGET
    else:
        color = COLOR_GOOD

    title = lot.title or f"Lot {lot.lot_number}"
    if len(title) > 240:
        title = title[:237] + "…"

    fields = [
        {"name": "Pris", "value": _format_price(match), "inline": True},
        {
            "name": "Lot",
            "value": f"nr. {lot.lot_number}" if lot.lot_number else "–",
            "inline": True,
        },
    ]

    if lot.ends_at:
        # Både hvor lang tid der er igen og det præcise tidspunkt: det første
        # afgør om man skal handle nu, det andet hvornår man skal sidde klar.
        left, _css, _secs = time_left(lot.ends_at.isoformat())
        value = f"{left}\n{lot.ends_at.strftime('%d/%m %H:%M')}" if left else \
            lot.ends_at.strftime("%d/%m %H:%M")
        fields.append({"name": "Hammerslag", "value": value, "inline": True})

    auction_title = lot.auction_title or "–"
    if len(auction_title) > 200:
        auction_title = auction_title[:197] + "…"
    fields.append({"name": "Auktion", "value": auction_title, "inline": False})

    if flags:
        labels = [FLAG_LABELS.get(flag, flag) for flag in flags]
        fields.append({"name": "Stand", "value": " · ".join(labels), "inline": False})

    if match.over_budget:
        fields.append(
            {
                "name": "Bemærk",
                "value": f"Over dit loft på {kr(match.category.max_price)}",
                "inline": False,
            }
        )

    embed = {
        # Ingen emoji: kategorien står i footeren, og et ikon i titlen er pynt
        # der skubber det, der betyder noget, ud til højre.
        "title": title,
        "url": lot.url or None,
        "color": color,
        "fields": fields,
        "footer": {"text": f"{match.category.label} · matchet på: {', '.join(match.keywords[:5])}"},
    }
    if lot.image_url and lot.image_url.startswith("http"):
        embed["thumbnail"] = {"url": lot.image_url}
    return {k: v for k, v in embed.items() if v is not None}


def build_payload(
    matches: list[Match],
    *,
    heading: str,
    username: str = "Auktionshuset Hunter",
    details: dict[str, tuple[str, ...]] | None = None,
) -> dict:
    """Byg en Discord-webhook-payload for op til 10 fund."""
    details = details or {}
    embeds = [
        build_embed(m, flags=details.get(m.lot.lot_id, ()))
        for m in matches[:MAX_EMBEDS_PER_MESSAGE]
    ]
    return {
        "username": username,
        "content": heading[:2000],
        "embeds": embeds,
        # Ingen roller eller brugere skal pinges af maskinelt genereret indhold.
        "allowed_mentions": {"parse": []},
    }


class DiscordNotifier:
    def __init__(self, webhook_url: str, *, timeout: int = 15, max_retries: int = 3) -> None:
        if not webhook_url:
            raise DiscordError("Webhook-URL er tom")
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()

    def _post(self, payload: dict) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(self.webhook_url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Netværksfejl mod Discord (forsøg %d/%d): %s", attempt, self.max_retries, exc)
            else:
                if response.status_code in (200, 204):
                    return
                if response.status_code == 429:
                    retry_after = 1.0
                    try:
                        retry_after = float(response.json().get("retry_after", 1.0))
                    except (ValueError, AttributeError):
                        pass
                    log.warning("Discord rate limit — venter %.1fs", retry_after)
                    last_error = DiscordError(f"rate limit, ventede {retry_after:.1f}s")
                    time.sleep(min(retry_after, 30))
                    continue
                if response.status_code in (500, 502, 503, 504):
                    last_error = DiscordError(f"HTTP {response.status_code}: {response.text[:200]}")
                else:
                    raise DiscordError(
                        f"Discord afviste beskeden (HTTP {response.status_code}): {response.text[:300]}"
                    )
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 10))
        raise DiscordError(f"Kunne ikke sende til Discord efter {self.max_retries} forsøg: {last_error}")

    def send_matches(
        self,
        matches: list[Match],
        *,
        heading: str,
        details: dict[str, tuple[str, ...]] | None = None,
    ) -> NotifyResult:
        """Send fund i grupper på højst 10 embeds pr. besked."""
        result = NotifyResult()
        for offset in range(0, len(matches), MAX_EMBEDS_PER_MESSAGE):
            chunk = matches[offset : offset + MAX_EMBEDS_PER_MESSAGE]
            payload = build_payload(chunk, heading=heading, details=details)
            try:
                self._post(payload)
            except DiscordError as exc:
                log.error("Kunne ikke sende %d fund: %s", len(chunk), exc)
                result.failed.extend(chunk)
                continue
            result.sent.extend(chunk)
            result.sent_messages += 1
        return result

    def send_text(self, content: str) -> bool:
        """Send en simpel tekstbesked, fx en fejlalarmering."""
        payload = {
            "username": "Auktionshuset Hunter",
            "content": content[:2000],
            "allowed_mentions": {"parse": []},
        }
        try:
            self._post(payload)
        except DiscordError as exc:
            log.error("Kunne ikke sende tekstbesked: %s", exc)
            return False
        return True

    def send_price_alerts(self, rows: list[PriceAlert]) -> bool:
        """Send ét samlet svar naar prisen er steget paa flere fulgte lots."""
        content = build_price_alerts(rows)
        if not content:
            return False
        return self.send_text(content)


def build_digest(rows: list[dict], *, max_items: int = 15) -> str:
    """Byg en tekstbesked med 'måske'-fund til samlet gennemsyn.

    Digestet er bevidst tekst og ikke embeds: det er en liste brugeren skal
    skimme og svare på, ikke noget der skal se indbydende ud. Titlerne
    afkortes, så beskeden kan læses på en telefon.
    """
    if not rows:
        return ""

    lines = [
        f"**{len(rows)} {'lot' if len(rows) == 1 else 'lots'} jeg er i tvivl om**"
        " — værd at kigge på:\n"
    ]
    for row in rows[:max_items]:
        title = (row.get("title") or "").strip()
        if len(title) > 110:
            title = title[:107] + "…"
        reason = (row.get("reason") or "").strip()
        url = (row.get("url") or "").strip()
        if url:
            lines.append(f"• [{title}]({url})")
        else:
            lines.append(f"• {title}")
        if reason:
            lines.append(f"  _{reason}_")

    if len(rows) > max_items:
        lines.append(f"\n… og {len(rows) - max_items} mere")

    text = "\n".join(lines)
    # Discord afviser beskeder over 2000 tegn. Skær hellere af end at fejle.
    if len(text) > 2000:
        text = text[:1990] + "\n…"
    return text


def send_digest(notifier: DiscordNotifier, rows: list[dict]) -> bool:
    """Send et digest af 'måske'-fund. Returnerer False hvis intet blev sendt."""
    content = build_digest(rows)
    if not content:
        return False
    return notifier.send_text(content)


def build_price_alerts(rows: list[PriceAlert], *, max_items: int = 12) -> str:
    """Én tekstbesked med de fulgte lots der er steget i pris.

    Samlet i stedet for én besked pr. lot: en travl auktion kan byde op flere
    gange i traek, og den slags skal ikke fylde kanalen.
    """
    if not rows:
        return ""

    lines = [
        f"**Prisen er steget på {len(rows)} "
        f"{'lot' if len(rows) == 1 else 'lots'} du følger**\n"
    ]
    for row in rows[:max_items]:
        title = (row.title or "ukendt").strip()
        if len(title) > 100:
            title = title[:97] + "…"
        before = kr(row.old_cost) if row.old_cost else "?"
        tail = ""
        if row.ends_at:
            left, _css, _secs = time_left(row.ends_at)
            if left:
                tail = f" · slutter om {left}"
        lines.append(
            f"• [{title}]({row.url}) — {before} -> {kr(row.new_cost)}{tail}"
            if row.url else f"• {title} — {before} -> {kr(row.new_cost)}{tail}"
        )

    if len(rows) > max_items:
        lines.append(f"\n… og {len(rows) - max_items} mere")

    text = "\n".join(lines)
    if len(text) > 2000:
        text = text[:1990] + "\n…"
    return text
