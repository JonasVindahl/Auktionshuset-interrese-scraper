"""Scraper for auktionshuset.dk.

Siden serverer al HTML server-rendered, så vi behøver hverken browser eller
JavaScript. To niveauer bruges:

* Auktionslisten  /auktioner/?...            -> hvilke auktioner findes der
* Kataloget       /auktioner/<slug>?page=N   -> de enkelte lots (varer)

Kataloger pagineres med ``limit`` (maks. 48 pr. side) og ``page`` (1-indekseret).
"""

from __future__ import annotations

import logging
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from .config import Source

log = logging.getLogger(__name__)

COPENHAGEN = ZoneInfo("Europe/Copenhagen")

# Serveren ignorerer limit > 48.
PAGE_SIZE = 48
MAX_PAGES = 200

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
}


class ScrapeError(RuntimeError):
    """Netværks- eller parsefejl under scraping."""


@dataclass(frozen=True)
class Auction:
    auction_id: str
    title: str
    url: str
    ends_text: str
    auction_type: str
    address_lines: tuple[str, ...]
    lot_count: int


@dataclass(frozen=True)
class Lot:
    lot_id: str
    title: str
    url: str
    lot_number: str
    auction_id: str
    auction_title: str
    current_bid: int | None
    total_price: int | None
    ends_at: datetime | None
    image_url: str
    has_bids: bool


def parse_danish_int(text: str | None) -> int | None:
    """'1.234,50' -> 1234. Forenkler til hele kroner."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", text.strip())
    if not cleaned:
        return None
    # Dansk format: '.' er tusindtal, ',' er decimal.
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return int(round(float(cleaned)))
    except ValueError:
        return None


def parse_ends(value: str | None) -> datetime | None:
    """Parse 'data-ends' (lokal dansk tid uden tidszone) til aware datetime."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=COPENHAGEN)
        except ValueError:
            continue
    log.debug("Ukendt data-ends-format: %r", value)
    return None


class Scraper:
    """Høflig HTTP-klient med retries og rate limiting."""

    def __init__(
        self,
        source: Source,
        *,
        delay_seconds: float = 1.0,
        timeout: int = 30,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.source = source
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def _get(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Netværksfejl (forsøg %d/%d) på %s: %s", attempt, self.max_retries, url, exc)
            else:
                if response.status_code == 200:
                    return response.text
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = ScrapeError(f"HTTP {response.status_code} på {url}")
                    log.warning(
                        "HTTP %d (forsøg %d/%d) på %s",
                        response.status_code,
                        attempt,
                        self.max_retries,
                        url,
                    )
                else:
                    raise ScrapeError(f"HTTP {response.status_code} på {url}")
            if attempt < self.max_retries:
                # Eksponentiel backoff med jitter — respekterer en travl server.
                time.sleep(min(2 ** attempt + random.uniform(0, 1), 30))
        raise ScrapeError(f"Gav op efter {self.max_retries} forsøg på {url}: {last_error}")

    def _polite_pause(self) -> None:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds + random.uniform(0, self.delay_seconds / 2))

    def auction_list_url(self, page: int = 1) -> str:
        params: list[tuple[str, str]] = [("search", ""), ("auctionStatus", str(self.source.auction_status))]
        for region_id in self.source.region_ids:
            params.append(("regions[]", region_id))
        if page > 1:
            params.append(("page", str(page)))
        return f"{self.source.base_url}/auktioner/?{urllib.parse.urlencode(params)}"

    def catalog_url(self, auction_url: str, page: int = 1) -> str:
        separator = "&" if "?" in auction_url else "?"
        return f"{auction_url}{separator}auctionStatus={self.source.auction_status}&limit={PAGE_SIZE}&page={page}"

    @staticmethod
    def _auction_type(soup: BeautifulSoup) -> str:
        badge = soup.select_one("p.bg-white.text-gray-401")
        return badge.get_text(strip=True) if badge else ""

    def fetch_auctions(self) -> list[Auction]:
        """Hent alle aktive auktioner for de valgte regioner."""
        html = self._get(self.auction_list_url())
        soup = BeautifulSoup(html, "lxml")
        auctions: list[Auction] = []

        for card in soup.select("li.loadmore-item"):
            auction_id = card.get("id")
            link = card.select_one("a[href^='/auktioner/']")
            if not auction_id or not link:
                continue
            href = link.get("href", "")
            if "/lots/" in href:
                continue

            title_el = card.select_one("h3")
            src = href if href.startswith("http") else f"{self.source.base_url}{href}"

            addresses = tuple(
                el.get_text(strip=True) for el in card.select("p.h4") if el.get_text(strip=True)
            )
            lot_count = 0
            for span in card.select("span"):
                match = re.match(r"(\d+)\s*lots?", span.get_text(strip=True))
                if match:
                    lot_count = int(match.group(1))
                    break

            date_el = card.select_one("p.text-xs.font-bold")

            auctions.append(
                Auction(
                    auction_id=str(auction_id),
                    title=title_el.get_text(strip=True) if title_el else "",
                    url=src,
                    ends_text=date_el.get_text(strip=True) if date_el else "",
                    auction_type=self._auction_type(card),
                    address_lines=addresses,
                    lot_count=lot_count,
                )
            )

        log.info("Fandt %d aktive auktioner (%s)", len(auctions), self.source.region_label)
        return auctions

    def fetch_lots(self, auction: Auction) -> list[Lot]:
        """Hent samtlige lots for én auktion ved at følge pagineringen."""
        lots: list[Lot] = []
        seen: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            html = self._get(self.catalog_url(auction.url, page))
            page_lots = self._parse_lots(html, auction)
            new = [lot for lot in page_lots if lot.lot_id not in seen]
            if not new:
                break
            lots.extend(new)
            seen.update(lot.lot_id for lot in new)

            if len(page_lots) < PAGE_SIZE:
                break
            self._polite_pause()

        log.info("  %s: %d lots", auction.title[:50] or auction.url, len(lots))
        return lots

    def _image_url(self, image_el) -> str:
        """Find billedets rigtige URL.

        Lazy-loadede billeder har en pladsholder i ``src`` og den rigtige adresse
        i ``data-src`` eller ``srcset``. Uden dette gemmer vi en 1x1-gif eller en
        base64-pladsholder, og dashboardet viser ingenting.
        """
        for attr in ("data-src", "data-lazy-src", "data-original", "src"):
            value = (image_el.get(attr) or "").strip()
            if value and not value.startswith("data:"):
                return self._absolute(value)

        # srcset: 'lille.jpg 300w, stor.jpg 900w' — tag den første adresse.
        srcset = (image_el.get("srcset") or "").strip()
        if srcset:
            first = srcset.split(",")[0].strip().split(" ")[0]
            if first and not first.startswith("data:"):
                return self._absolute(first)
        return ""

    def _absolute(self, url: str) -> str:
        """Gør en relativ adresse absolut, så browseren kan hente billedet."""
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"{self.source.base_url}{url}"
        return url

    def _parse_lots(self, html: str, auction: Auction) -> list[Lot]:
        soup = BeautifulSoup(html, "lxml")
        lots: list[Lot] = []

        for item in soup.select("li.lot-item"):
            lot_id = item.get("id")
            if not lot_id:
                continue
            lot_id = str(lot_id)

            detail_link = item.select_one("a[href*='/lots/']")
            href = detail_link.get("href", "") if detail_link else ""
            if href.startswith("/"):
                href = f"{self.source.base_url}{href}"

            lot_number = ""
            number_el = item.find("p", class_=lambda c: c and "text-xs" in c and "text-secondary" in c)
            if number_el:
                text = number_el.get_text(strip=True)
                match = re.search(r"Lot nr\.?\s*(\d+)", text)
                if match:
                    lot_number = match.group(1)

            title = ""
            title_el = item.select_one("h3")
            if title_el:
                title = title_el.get_text(" ", strip=True)

            amount_el = item.select_one(".bid-amount")
            total_el = item.select_one(".bid-amount-total")
            current_bid = parse_danish_int(amount_el.get_text(strip=True) if amount_el else None)
            total_price = parse_danish_int(total_el.get_text(strip=True) if total_el else None)
            if total_price is None:
                # Uden bud er totalfeltet tomt — brug buddet som bedste estimat.
                total_price = current_bid

            image_el = item.select_one("img")
            image_url = self._image_url(image_el) if image_el else ""

            classes = item.get("class") or []

            lots.append(
                Lot(
                    lot_id=lot_id,
                    title=title,
                    url=href,
                    lot_number=lot_number,
                    auction_id=auction.auction_id,
                    auction_title=auction.title,
                    current_bid=current_bid,
                    total_price=total_price,
                    ends_at=parse_ends(item.get("data-ends")),
                    image_url=image_url,
                    has_bids="item-bid" in classes,
                )
            )

        return lots
