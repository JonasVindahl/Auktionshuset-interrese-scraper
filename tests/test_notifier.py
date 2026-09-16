"""Tests for Discord-payloads. Ingen netværkskald."""

from auction_hunter.config import load_config
from auction_hunter.matcher import match_lot
from auction_hunter.notifier import (
    MAX_EMBEDS_PER_MESSAGE,
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

    with pytest.raises(Exception):
        DiscordNotifier("")
