"""Tests for prisberegning baseret på verificerede tal fra auktionshuset.dk."""

import pytest

from auction_hunter.fees import (
    MINIMUM_FEE,
    estimate,
    fee_for,
    is_vat_exempt,
    total_for,
)

# (bud, forventet total) — aflæst direkte på auktionshuset.dk.
VERIFIED_NORMAL = [
    (25, 94),
    (50, 125),
    (75, 156),
    (100, 188),
    (125, 219),
    (150, 250),
    (175, 281),
    (200, 312),
    (225, 344),
    (250, 375),
    (275, 412),
    (325, 488),
    (500, 750),
    (1000, 1500),
    (2800, 4200),
    (5200, 7800),
    (23000, 34500),
    (70000, 105000),
]

# Momsfri auktion ("Ophørt VVS firma") — salæret er momsbelagt, hammerprisen ikke.
VERIFIED_VAT_EXEMPT = [(50, 112), (75, 138), (100, 162), (150, 212), (200, 262)]


@pytest.mark.parametrize("bid,expected", VERIFIED_NORMAL)
def test_total_matches_site_for_normal_auctions(bid, expected):
    assert total_for(bid) == expected


@pytest.mark.parametrize("bid,expected", VERIFIED_VAT_EXEMPT)
def test_total_matches_site_for_vat_exempt_auctions(bid, expected):
    assert total_for(bid, vat_exempt=True) == expected


def test_minimum_fee_dominates_small_bids():
    """Under 250 kr er det faste mindstesalær på 50 kr styrende."""
    assert MINIMUM_FEE == 50
    for bid in (25, 50, 100, 200, 249):
        assert fee_for(bid) == 50


def test_fee_switches_to_percentage_above_threshold():
    # 20% af 250 = 50, altså præcis hvor satsen skifter.
    assert fee_for(250) == 50
    assert fee_for(500) == 100
    assert fee_for(1000) == 200


def test_no_bid_gets_entry_cost_not_zero():
    """Et lot uden bud må ikke fremstå gratis."""
    est = estimate(None)
    assert est.current_total is None
    assert est.is_estimate
    assert est.entry_cost == total_for(50) == 125


def test_current_bid_uses_real_total():
    est = estimate(200)
    assert est.current_bid == 200
    assert est.current_total == 312
    assert not est.is_estimate


def test_vat_exemption_detected_from_auction_title():
    assert is_vat_exempt("Ophørt VVS firma - Maskiner, Værktøj , Udstyr (momsfri)")
    assert is_vat_exempt("Konkursauktion - uden moms")
    assert not is_vat_exempt("Selskab af 31. juli 2026 A/S")
    assert not is_vat_exempt("")


def test_vat_exempt_entry_cost_is_lower():
    normal = estimate(None, auction_title="Almindelig auktion")
    exempt = estimate(None, auction_title="Auktion (momsfri)")
    assert exempt.entry_cost < normal.entry_cost
    assert exempt.vat_exempt
