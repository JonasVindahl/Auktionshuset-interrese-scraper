"""Tests for parsere — kører på gemt HTML, så de ikke rammer netværket."""

from pathlib import Path

import pytest

from auction_hunter.scraper import (
    Scraper,
    parse_danish_int,
    parse_ends,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.234,50", 1234),
        ("1.234", 1234),
        ("125", 125),
        ("0,00", 0),
        ("12.345,00", 12345),
        ("75 kr", 75),
        (None, None),
        ("", None),
        ("ingen tal", None),
    ],
)
def test_parse_danish_int(text, expected):
    assert parse_danish_int(text) == expected


def test_parse_ends_treats_time_as_copenhagen():
    parsed = parse_ends("2026-09-16 14:00:00")
    assert parsed is not None
    assert parsed.hour == 14
    assert parsed.tzinfo is not None


def test_parse_ends_without_seconds():
    parsed = parse_ends("2026-09-16 14:00")
    assert parsed is not None and parsed.minute == 0


def test_parse_ends_rejects_nonsense():
    assert parse_ends("ikke en dato") is None
    assert parse_ends(None) is None


def _make_scraper() -> Scraper:
    from auction_hunter.config import Source

    return Scraper(Source(), delay_seconds=0)


def test_catalog_url_includes_pagination():
    """Paginering sker med limit/page — serveren ignorerer limit over 48."""
    scraper = _make_scraper()
    url = scraper.catalog_url("https://auktionshuset.dk/auktioner/test", page=3)
    assert "limit=48" in url
    assert "page=3" in url
    assert "auctionStatus=1" in url


def test_auction_list_url_includes_region_filter():
    scraper = _make_scraper()
    url = scraper.auction_list_url()
    assert "regions%5B%5D=lyr0boj4d8" in url
    assert "auctionStatus=1" in url


@pytest.mark.skipif(
    not (FIXTURES / "catalog.html").exists(),
    reason="kræver gemt katalog-HTML (tests/fixtures/catalog.html)",
)
def test_parse_lots_from_saved_html():
    from auction_hunter.scraper import Auction

    html = (FIXTURES / "catalog.html").read_text(encoding="utf-8")
    scraper = _make_scraper()
    auction = Auction(
        auction_id="test",
        title="Testauktion",
        url="https://auktionshuset.dk/auktioner/test",
        ends_text="",
        auction_type="Konkursauktion",
        address_lines=(),
        lot_count=0,
    )
    lots = scraper._parse_lots(html, auction)
    assert lots, "forventede mindst ét lot i fixture"
    first = lots[0]
    assert first.lot_id
    assert first.title
    assert first.url.startswith("http")
    assert first.ends_at is not None
