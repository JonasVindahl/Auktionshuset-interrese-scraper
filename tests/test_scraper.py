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


def _auction_card(auction_id: str, title: str, lots: int = 10) -> str:
    """Et kort som auktionslisten serverer det."""
    return (
        f'<li class="loadmore-item" id="{auction_id}">'
        f'<a href="/auktioner/{auction_id}"><h3>{title}</h3></a>'
        f'<span>{lots} lots</span>'
        f'<p class="text-xs font-bold">1. januar</p>'
        f"</li>"
    )


def _auction_page(ids: list[str]) -> str:
    cards = "".join(_auction_card(i, f"Auktion {i}") for i in ids)
    return f"<html><body><ul>{cards}</ul></body></html>"


def test_fetch_auctions_follows_pagination():
    """Auktionslisten pagineres, så side 2 og frem skal med.

    Uden det ser agenten kun de første 24 auktioner. Det ligner ikke en fejl,
    fordi lot-antallet stadig er stort, så blindheds-tjekket tier.
    """
    scraper = _make_scraper()
    pages = {
        1: _auction_page([f"a{i}" for i in range(24)]),
        2: _auction_page([f"b{i}" for i in range(24)]),
        3: _auction_page([f"c{i}" for i in range(5)]),
    }
    seen_urls: list[str] = []

    def fake_get(url: str) -> str:
        seen_urls.append(url)
        page = 1
        if "page=" in url:
            page = int(url.split("page=")[1].split("&")[0])
        return pages.get(page, _auction_page([]))

    scraper._get = fake_get
    auctions = scraper.fetch_auctions()

    assert len(auctions) == 53, f"forventede alle tre sider, fik {len(auctions)}"
    assert {a.auction_id for a in auctions} >= {"a0", "b0", "c4"}
    assert len(seen_urls) >= 3


def test_fetch_auctions_stops_without_new_cards():
    """En server der svarer med samme side igen må ikke give en uendelig løkke."""
    scraper = _make_scraper()
    same = _auction_page([f"a{i}" for i in range(24)])
    calls = {"n": 0}

    def fake_get(url: str) -> str:
        calls["n"] += 1
        return same

    scraper._get = fake_get
    auctions = scraper.fetch_auctions()

    assert len(auctions) == 24
    assert calls["n"] <= 3, "måtte ikke blive ved med at hente den samme side"


def test_user_agent_kan_saettes_uden_kodeaendring(monkeypatch):
    """Standarden udgiver sig for Chrome, og det er ejerens valg at aendre.

    Strengen skal derfor kunne saettes fra miljoeet, saa en aerlig
    User-Agent med kontaktadresse ikke kraever en kodeaendring.
    """
    from auction_hunter.scraper import DEFAULT_USER_AGENT

    monkeypatch.delenv("SCRAPER_USER_AGENT", raising=False)
    assert _make_scraper().session.headers["User-Agent"] == DEFAULT_USER_AGENT

    monkeypatch.setenv("SCRAPER_USER_AGENT", "hunter/1.1 (+mig@eksempel.dk)")
    assert _make_scraper().session.headers["User-Agent"] == "hunter/1.1 (+mig@eksempel.dk)"


# -- auktionsinfo: adresse og levering --------------------------------------

def _panel(shipping: bool) -> str:
    levering = "Forsendelse tilgængelig" if shipping else "Forsendelse ikke tilgængelig"
    return f"""<html><body>
      <div class="dropdown-body">
        <div><p class="opacity-75">Auktionsadresse</p>
             <p class="font-heading">Vej 1<br/>DK-8361 Hasselager</p></div>
        <div><p class="opacity-75">Eftersyn</p><p class="font-heading">Ingen</p></div>
        <div class="flex flex-col gap-2">
          <div class="flex gap-1 items-center">
            <div title="Det er ikke muligt at faa tilsendt varer"><div></div></div>
            <div>{levering}</div>
          </div>
        </div>
      </div>
    </body></html>"""


def test_parse_auction_info_henter_adresse_og_forsendelse():
    scraper = _make_scraper()
    assert scraper._parse_auction_info(_panel(True)) == {
        "address": "Vej 1 DK-8361 Hasselager", "shipping": True,
    }
    assert scraper._parse_auction_info(_panel(False))["shipping"] is False


def test_parse_auction_info_uden_panel_giver_ingen_felter():
    assert _make_scraper()._parse_auction_info("<html><body><p>tom</p></body></html>") == {}


def test_parse_auctions_maerker_region():
    scraper = _make_scraper()
    html = _auction_page(["a1"])
    auctions = scraper._parse_auctions(html, "Sjælland")
    assert auctions and auctions[0].region == "Sjælland"


def test_fetch_auctions_henter_en_landdel_ad_gangen():
    """Regionen staar ikke paa kortet, saa den kommer fra det kald der gav det."""
    from auction_hunter.config import Source

    scraper = Scraper(
        Source(region_ids=("r1", "r2"), region_names={"r1": "Sjælland", "r2": "Fyn"}),
        delay_seconds=0,
    )

    def fake_get(url: str) -> str:
        if "r1" in url:
            return _auction_page(["a1"])
        if "r2" in url:
            return _auction_page(["b1"])
        return _auction_page([])

    scraper._get = fake_get
    auctions = scraper.fetch_auctions()
    assert {(a.auction_id, a.region) for a in auctions} == {
        ("a1", "Sjælland"), ("b1", "Fyn"),
    }
