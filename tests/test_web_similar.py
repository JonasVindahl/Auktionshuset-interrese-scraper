"""Tests for "Lignende salg".

Arkivet er fuldt af lots der deler et enkelt ord — "computer", "lenovo", et
"div."-lot — og de er ikke sammenlignelige. Testene vogter at et maerke eller
en kategori alene ikke er nok, mens et modelnummer og et boejet ord stadig er.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from auction_hunter.scraper import Lot
from auction_hunter.storage import Store
from auction_hunter.web import similar


def _ended_lot(lot_id: str, title: str, bid: int = 100, total: int = 150) -> Lot:
    return Lot(
        lot_id=lot_id, title=title, url=f"https://x/{lot_id}", lot_number=lot_id,
        auction_id="a1", auction_title="Testauktion", current_bid=bid,
        total_price=total, ends_at=datetime.now(UTC) - timedelta(hours=48),
        image_url="", has_bids=True,
    )


@pytest.fixture()
def store():
    lots = [
        _ended_lot("s", "Computer LENOVO ThinkCentre M720q", 300, 400),
        _ended_lot("sk", "Computerskaerm 24' tommer LENOVO LEGION Model: R24e", 600, 900),
        _ended_lot("del", "Div. Computer reserverdele: harddiske, kabler mv. SYNOLOGY/HP", 300, 450),
        _ended_lot("ud", "Div. Computerudstyr: kabler, battrier, stander mv. VIDEO DEVICES", 350, 525),
        _ended_lot("sup", "Div. Computer reserverdele: PC-blaeser mv. SUPERMICRO Mod", 100, 150),
        _ended_lot("soes", "Lenovo ThinkCentre M710q Tiny stationaer pc", 500, 700),
        _ended_lot("mb", "Supermicro X11DPi bundkort", 400, 550),
        _ended_lot("kun", "Kun X11DPi", 200, 300),
        _ended_lot("h1", "Hoejttaler B&O Beolab", 100, 150),
        _ended_lot("h2", "Hoejttalere B&O Beolab 8000", 200, 300),
    ]
    with Store(":memory:") as s:
        s.record_lots(lots, {lot.lot_id: lot.total_price for lot in lots})
        yield s


def _titles(comps) -> list[str]:
    return [sale.title for sale in comps.sales]


def test_maerke_eller_kategori_alene_er_ikke_nok(store):
    """En skaerm og en reservedelsbunke deler ord med en pc, men er ikke en pc."""
    comps = similar.find(
        store.conn, lot_id="s", title="Computer LENOVO ThinkCentre M720q"
    )
    assert _titles(comps) == ["Lenovo ThinkCentre M710q Tiny stationaer pc"]


def test_kun_delt_ord_er_ikke_nok(store):
    """'computer' maa ikke ramme 'computerudstyr' eller 'computerskaerm'."""
    comps = similar.find(
        store.conn, lot_id="s", title="Computer LENOVO ThinkCentre M720q"
    )
    assert all("Div." not in title for title in _titles(comps))
    assert all("skaerm" not in title.lower() for title in _titles(comps))


def test_modelnummer_baerer_et_match_alene(store):
    """Et modelnummer er praecist nok til at staa alene, i modsaetning til et maerke."""
    comps = similar.find(
        store.conn, lot_id="mb", title="Supermicro X11DPi bundkort"
    )
    assert "Kun X11DPi" in _titles(comps)


def test_modelnummer_henter_ikke_maerke_soesken_ind(store):
    """Det delte modelnummer maa ikke traekke maerkets andre dele med."""
    comps = similar.find(
        store.conn, lot_id="mb", title="Supermicro X11DPi bundkort"
    )
    assert all("SUPERMICRO Mod" not in title for title in _titles(comps))


def test_boejning_taeller_som_samme_ord(store):
    comps = similar.find(store.conn, lot_id="h1", title="Hoejttaler B&O Beolab")
    assert "Hoejttalere B&O Beolab 8000" in _titles(comps)
