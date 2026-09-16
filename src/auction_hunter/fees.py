"""Beregning af den reelle købspris inkl. moms og salær.

Verificeret mod 280+ prismærker fra auktionshuset.dk:

    Normal auktion:  total = (bud + salær) * 1,25
    Momsfri auktion: total =  bud + salær * 1,25

hvor ``salær = max(50, 20% af bud)``.

Bemærk at lot-totalen på siden for et lot uden bud er 0. Den pris er ikke
meningsfuld som "gratis" — et bud starter et sted og kan kun gå op. Derfor
beregnes i stedet en ``entry_cost``: hvad det koster at være den første, der
byder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Mindstesalæret. Et bud på 50 kr koster derfor reelt 125 kr inkl. moms.
MINIMUM_FEE = 50
FEE_RATE = 0.20
VAT_RATE = 0.25

# Første bud på et lot uden bud. Auktionshuset starter typisk budrunden her.
DEFAULT_OPENING_BID = 50

_MOMSFRI = re.compile(r"momsfri|momsfrit|uden\s+moms", re.IGNORECASE)


def is_vat_exempt(auction_title: str) -> bool:
    """Momsfri auktioner har en anden prisformel end de øvrige."""
    return bool(_MOMSFRI.search(auction_title or ""))


@dataclass(frozen=True)
class PriceEstimate:
    """Den reelle pris for et lot, samt hvordan den er fremkommet."""

    current_bid: int | None
    current_total: int | None
    entry_cost: int
    vat_exempt: bool

    @property
    def is_estimate(self) -> bool:
        """True hvis prisen er et bud-estimat og ikke et faktisk bud."""
        return not self.current_bid


def fee_for(bid: int) -> int:
    """Salær i kroner før moms."""
    return int(round(max(MINIMUM_FEE, bid * FEE_RATE)))


def total_for(bid: int, *, vat_exempt: bool = False, include_fee_vat: bool = True) -> int:
    """Reel pris inkl. moms og salær for et givet bud."""
    fee = fee_for(bid)
    fee_with_vat = fee * (1 + VAT_RATE) if include_fee_vat else fee
    if vat_exempt:
        # Hammerprisen er momsfri, men salæret er altid momsbelagt.
        return int(round(bid + fee_with_vat))
    return int(round((bid + fee) * (1 + VAT_RATE)))


def estimate(
    current_bid: int | None,
    *,
    auction_title: str = "",
    opening_bid: int = DEFAULT_OPENING_BID,
) -> PriceEstimate:
    """Estimer den reelle pris for et lot.

    For lots uden bud beregnes hvad første bud vil koste inkl. moms og salær,
    så et lot uden bud ikke fejlagtigt fremstår som gratis.
    """
    vat_exempt = is_vat_exempt(auction_title)

    if current_bid and current_bid > 0:
        return PriceEstimate(
            current_bid=current_bid,
            current_total=total_for(current_bid, vat_exempt=vat_exempt),
            entry_cost=total_for(opening_bid, vat_exempt=vat_exempt),
            vat_exempt=vat_exempt,
        )

    return PriceEstimate(
        current_bid=None,
        current_total=None,
        entry_cost=total_for(opening_bid, vat_exempt=vat_exempt),
        vat_exempt=vat_exempt,
    )
