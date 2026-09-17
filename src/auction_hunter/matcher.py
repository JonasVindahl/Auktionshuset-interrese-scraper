"""Matchning af lots mod interesseprofilen.

Prisen der vurderes er den reelle købspris inkl. moms og salær, fordi det er
den brugeren faktisk betaler. For lots uden bud findes ingen faktisk pris —
der bruges i stedet prisen for at afgive første bud, så et lot ikke fejlagtigt
fremstår som gratis. Loftet er blødt: fund over grænsen rapporteres stadig,
men markeres, indtil de passerer ``max_price * soft_over_budget_factor``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .config import Category, Config, Profile
from .fees import PriceEstimate, estimate
from .scraper import Lot
from .textmatch import find_keywords


@dataclass(frozen=True)
class Match:
    lot: Lot
    category: Category
    keywords: tuple[str, ...]
    price: PriceEstimate
    over_budget: bool
    profile: Profile | None = None

    @property
    def profile_key(self) -> str:
        """Noeglen paa den profil fundet kom fra.

        Uden profiler er der én implicit standardprofil, så notifikationer fra
        foer profilerne beholder deres noegle og ikke sendes igen.
        """
        return self.profile.key if self.profile else "standard"

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
    now = now or datetime.now(UTC)
    remaining = (lot.ends_at - now).total_seconds() / 3600
    return 0 <= remaining <= within_hours


def match_lot(
    lot: Lot,
    config: Config,
    *,
    opening_bid: int | None = None,
    profile: Profile | None = None,
) -> list[Match]:
    """Find alle kategorier et lot matcher, med respekt for prisloftet.

    Et lot kan matche flere kategorier. Titlen er den primære signalkilde;
    auktionsnavnet bruges kun til at afgøre momsforhold, ikke til at matche
    nøgleord — ellers ville hvert lot i en "IT-udstyr"-auktion blive et match.

    Med en profil indsnævres kategorierne, og profilens loft og udelukkelser
    gælder i stedet for topniveauets. Uden profil er adfærden uændret.
    """
    haystack = lot.title.lower()
    if lot.lot_number:
        haystack = f"lot {lot.lot_number} {haystack}"

    # Eksplicitte udelukkelser slår altid igennem. Globale gælder også for en
    # profil, som kun kan lægge sine egne oveni.
    if find_keywords(haystack, config.profile_exclude(profile)):
        return []

    # Et lot uden for hjemlandsdelene er kun et fund hvis auktionen kan sende.
    # Ellers ville et landsdaekkende scrape give stoj fra varer man ikke kan
    # hente. En ukendt region slippes igennem, saa et aendret HTML-udtraek
    # ikke koster fund.
    if not config.source.is_local_region(lot.region) and not lot.shipping:
        return []

    price = estimate(
        lot.current_bid,
        auction_title=lot.auction_title,
        **({"opening_bid": opening_bid} if opening_bid else {}),
    )
    cost = price.current_total or price.entry_cost

    soft = (
        profile.soft_over_budget_factor
        if profile is not None and profile.soft_over_budget_factor is not None
        else config.soft_over_budget_factor
    )
    profile_budget = profile.max_price if profile is not None else None

    matches: list[Match] = []
    for category in config.profile_categories(profile):
        is_match, keywords = category.match(haystack)
        if not is_match:
            continue

        budget = profile_budget if profile_budget is not None else category.max_price
        if cost > budget * soft:
            continue

        matches.append(
            Match(
                lot=lot,
                category=category,
                keywords=keywords,
                price=price,
                over_budget=cost > budget,
                profile=profile,
            )
        )

    return matches


def match_all(
    lots: list[Lot],
    config: Config,
    *,
    opening_bid: int | None = None,
    profile: Profile | None = None,
) -> list[Match]:
    results: list[Match] = []
    for lot in lots:
        results.extend(match_lot(lot, config, opening_bid=opening_bid, profile=profile))
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
