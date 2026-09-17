"""Tests for Discord-payloads. Ingen netværkskald."""

from datetime import UTC

from auction_hunter.config import load_config
from auction_hunter.matcher import match_lot
from auction_hunter.notifier import (
    MAX_EMBEDS_PER_MESSAGE,
    DiscordError,
    DiscordNotifier,
    build_embed,
    build_payload,
)
from auction_hunter.scraper import Lot


def make_lot(title, *, bid=None, auction_title="Testauktion", lot_id="L1"):
    return Lot(
        lot_id=lot_id, title=title,
        url="https://auktionshuset.dk/auktioner/test/lots/1/x",
        lot_number="7", auction_id="A1", auction_title=auction_title,
        current_bid=bid, total_price=None, ends_at=None,
        image_url="", has_bids=bool(bid),
    )


def first_match(title, **kwargs):
    config = load_config("config/interests.yml")
    matches = match_lot(make_lot(title, **kwargs), config)
    assert matches, f"forventede match for {title!r}"
    return matches[0]


def test_embed_has_expected_shape():
    embed = build_embed(first_match("Forstærker YAMAHA A-S301", bid=500))
    assert embed["title"]
    assert embed["color"]
    assert embed["url"]
    names = {f["name"] for f in embed["fields"]}
    assert {"Pris", "Lot", "Auktion"} <= names


def test_embed_marks_estimate_for_lot_without_bids():
    embed = build_embed(first_match("DALI højttalere"))
    price_field = next(f for f in embed["fields"] if f["name"] == "Pris")
    assert "estimat" in price_field["value"].lower()
    assert "~" in price_field["value"]


def test_embed_marks_over_budget():
    config = load_config("config/interests.yml")
    coffee = config.category("coffee")
    bid = int(coffee.max_price * 1.5)
    matches = match_lot(make_lot("Espressomaskine RANCILIO Silvia", bid=bid), config)
    embed = build_embed(matches[0])
    names = {f["name"] for f in embed["fields"]}
    assert "Bemærk" in names


def test_embed_notes_vat_exempt_auction():
    match = first_match(
        "Forstærker YAMAHA A-S301", bid=500,
        auction_title="Ophørt VVS firma (momsfri)",
    )
    price_field = next(f for f in build_embed(match)["fields"] if f["name"] == "Pris")
    assert "momsfri" in price_field["value"].lower()


def test_payload_never_pings_anyone():
    """Maskingenereret indhold må ikke kunne trigge @everyone."""
    match = first_match("Forstærker YAMAHA A-S301", bid=500)
    payload = build_payload([match], heading="@everyone se her")
    assert payload["allowed_mentions"] == {"parse": []}


def test_payload_caps_embeds_per_message():
    matches = [first_match("Forstærker YAMAHA A-S301", bid=500, lot_id=f"L{i}") for i in range(25)]
    payload = build_payload(matches, heading="test")
    assert len(payload["embeds"]) == MAX_EMBEDS_PER_MESSAGE


def test_long_title_is_truncated():
    long_title = "Forstærker YAMAHA " + "x" * 400
    embed = build_embed(first_match(long_title, bid=500))
    assert len(embed["title"]) <= 250


def test_notifier_rejects_empty_webhook():
    import pytest

    with pytest.raises(DiscordError):
        DiscordNotifier("")


class TestDigest:
    """Digestet samler 'måske'-fund, så de ikke støjer i nuet."""

    def test_empty_list_gives_no_message(self):
        from auction_hunter.notifier import build_digest

        assert build_digest([]) == ""

    def test_lists_title_reason_and_link(self):
        from auction_hunter.notifier import build_digest

        rows = [
            {
                "title": "Div. lydudstyr: DVI-forlængere",
                "reason": "Rodlot med adaptere",
                "url": "https://auktionshuset.dk/lot/1",
            }
        ]
        text = build_digest(rows)
        assert "1 lot jeg er i tvivl om" in text
        assert "Div. lydudstyr" in text
        assert "Rodlot med adaptere" in text
        assert "https://auktionshuset.dk/lot/1" in text

    def test_caps_items_and_reports_remainder(self):
        from auction_hunter.notifier import build_digest

        rows = [{"title": f"Lot {i}", "reason": "", "url": ""} for i in range(40)]
        text = build_digest(rows, max_items=5)
        assert "og 35 mere" in text

    def test_never_exceeds_discord_limit(self):
        from auction_hunter.notifier import build_digest

        rows = [
            {"title": "T" * 300, "reason": "R" * 300, "url": "u" * 300}
            for _ in range(30)
        ]
        assert len(build_digest(rows)) <= 2000

    def test_long_title_is_shortened(self):
        from auction_hunter.notifier import build_digest

        rows = [{"title": "x" * 400, "reason": "", "url": ""}]
        assert "…" in build_digest(rows)

    def test_send_digest_returns_false_when_empty(self):
        from auction_hunter.notifier import send_digest

        assert send_digest(DiscordNotifier("https://discord.invalid/hook"), []) is False

# -- embed-indhold ----------------------------------------------------------

def make_match(title, *, bid=500, hours=3, auction_title="Testauktion"):
    """Et match med en sluttid, saa tid-tilbage kan testes."""
    from datetime import datetime, timedelta

    ends = datetime.now(UTC) + timedelta(hours=hours) if hours else None
    lot = Lot(
        lot_id="E1", title=title, url="https://auktionshuset.dk/lots/1",
        lot_number="7", auction_id="A1", auction_title=auction_title,
        current_bid=bid, total_price=None, ends_at=ends,
        image_url="", has_bids=bool(bid),
    )
    config = load_config("config/interests.yml")
    matches = match_lot(lot, config, opening_bid=config.opening_bid)
    assert matches, f"forventede match for {title!r}"
    return matches[0]


def test_embed_har_ingen_emoji_i_titlen():
    """Emoji i titlen skubber det vigtige ud til hoejre og er ren pynt."""
    embed = build_embed(make_match("Forstærker YAMAHA A-S301", bid=500))
    emoji = [c for c in embed["title"] if ord(c) > 0x2100]
    assert not emoji, f"emoji i titlen: {emoji}"


def test_embed_viser_både_tid_tilbage_og_tidspunkt():
    embed = build_embed(make_match("Forstærker YAMAHA A-S301", bid=500, hours=3))
    felt = next(f for f in embed["fields"] if f["name"] == "Hammerslag")
    assert felt["value"].startswith("Slutter om")
    assert "/" in felt["value"], "det præcise tidspunkt mangler"


def test_embed_uden_bud_er_markeret_som_estimat():
    embed = build_embed(make_match("Forstærker YAMAHA A-S301", bid=None))
    pris = next(f for f in embed["fields"] if f["name"] == "Pris")
    assert "estimat" in pris["value"]
    assert "~" in pris["value"]


def test_embed_skriver_loftet_med_dansk_tusindtal():
    embed = build_embed(make_match("Forstærker YAMAHA A-S301", bid=3000))
    tekst = " ".join(f["value"] for f in embed["fields"])
    assert "kr" in tekst
    assert "," not in tekst, "tusindtal skal skrives med punktum"


def test_prisadvarsel_naevner_titel_og_beloeb():
    from auction_hunter.notifier import PriceAlert, build_price_alerts

    text = build_price_alerts([PriceAlert(
        lot_id="L1", title="Synology DS1817+ NAS", url="https://x/1",
        old_cost=1250, new_cost=1450, ends_at=None,
    )])
    assert "Synology" in text
    assert "1.250" in text and "1.450" in text


def test_tom_prisadvarsel_giver_ingen_besked():
    from auction_hunter.notifier import build_price_alerts
    assert build_price_alerts([]) == ""


def test_sidste_chance_naevner_titel_og_tid():
    from auction_hunter.notifier import LastChanceAlert, build_last_chance

    text = build_last_chance([LastChanceAlert(
        lot_id="L1", title="Thorens TD160 pladespiller", url="https://x/1",
        ends_at=None,
    )])
    assert "Thorens" in text
    assert "slutter snart" in text


def test_tom_sidste_chance_giver_ingen_besked():
    from auction_hunter.notifier import build_last_chance
    assert build_last_chance([]) == ""
