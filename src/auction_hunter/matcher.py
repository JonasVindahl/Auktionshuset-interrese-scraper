"""Matchning af lots mod interesseprofilen.

Prisen der vurderes er den reelle købspris inkl. moms og salær, fordi det er
den brugeren faktisk betaler. For lots uden bud findes ingen faktisk pris —
der bruges i stedet prisen for at afgive første bud, så et lot ikke fejlagtigt
fremstår som gratis. Loftet er blødt: fund over grænsen rapporteres stadig,
men markeres, indtil de passerer ``max_price * soft_over_budget_factor``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Category, Config
from .fees import PriceEstimate, estimate
from .scraper import Lot


@dataclass(frozen=True)
class Match:
    lot: Lot
    category: Category
    keywords: tuple[str, ...]
    price: PriceEstimate
    over_budget: bool

    @property
    def cost(self) -> int:
        """Den pris der sammenlignes med budgettet."""
        return self.price.current_total or self.price.entry_cost

    @property
    def is_estimate(self) -> bool:
        return self.price.is_estimate


def last_chance(lot: Lot, within_hours: float, now: datetime | None = None) -> bool:
    """Bruges til at fremhæve fund tæt på hammerslag."""
    if lot.ends_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    remaining = (lot.ends_at - now).total_seconds() / 3600
    return 0 <= remaining <= within_hours


def match_lot(lot: Lot, config: Config, *, opening_bid: int | None = None) -> list[Match]:
    """Find alle kategorier et lot matcher, med respekt for prisloftet.

    Et lot kan matche flere kategorier. Titlen er den primære signalkilde;
    auktionsnavnet bruges kun til at afgøre momsforhold, ikke til at matche
    nøgleord — ellers ville hvert lot i en "IT-udstyr"-auktion blive et match.
    """
    haystack = lot.title.lower()
    if lot.lot_number:
        haystack = f"lot {lot.lot_number} {haystack}"

    # Eksplicitte udelukkelser slår altid igennem.
    if config.is_excluded(haystack):
        return []

    price = estimate(
        lot.current_bid,
        auction_title=lot.auction_title,
        **({"opening_bid": opening_bid} if opening_bid else {}),
    )
    cost = price.current_total or price.entry_cost

    matches: list[Match] = []
    for category in config.categories:
        is_match, keywords = category.match(haystack)
        if not is_match:
            continue

        ceiling = category.max_price * config.soft_over_budget_factor
        if cost > ceiling:
            continue

        matches.append(
            Match(
                lot=lot,
                category=category,
                keywords=keywords,
                price=price,
                over_budget=cost > category.max_price,
            )
        )

    return matches


def match_all(lots: list[Lot], config: Config, *, opening_bid: int | None = None) -> list[Match]:
    results: list[Match] = []
    for lot in lots:
        results.extend(match_lot(lot, config, opening_bid=opening_bid))
    return results


def sort_matches(matches: list[Match]) -> list[Match]:
    """Billigst først, derefter dem der slutter snarest.

    Fund uden bud sorteres efter deres entry-cost, så de ligger korrekt
    blandt de øvrige i stedet for at fremstå gratis.
    """
    def key(m: Match):
        ends = m.lot.ends_at.timestamp() if m.lot.ends_at else float("inf")
        return (m.cost, ends)

    return sorted(matches, key=key)
