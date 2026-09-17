"""Fælles fixtures til testene.

Web-testene importerer FastAPI via ``pytest.importorskip`` i deres egne filer,
så agentens tests kører uden web-afhængighederne installeret.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from auction_hunter.scraper import Lot
from auction_hunter.storage import Store

UTC = UTC
REPO_ROOT = Path(__file__).resolve().parent.parent


def make_lot(
    lot_id: str,
    title: str,
    *,
    ends_in_hours: float | None = 24,
    image: str = "",
    first_bid: int = 100,
    total: int = 150,
    auction_id: str = "a1",
    auction_title: str = "Testauktion",
) -> Lot:
    ends = (
        datetime.now(UTC) + timedelta(hours=ends_in_hours)
        if ends_in_hours is not None else None
    )
    return Lot(
        lot_id=lot_id, title=title,
        url=f"https://auktionshuset.dk/lots/{lot_id}",
        lot_number=lot_id[-1], auction_id=auction_id, auction_title=auction_title,
        current_bid=first_bid, total_price=total, ends_at=ends,
        image_url=image, has_bids=True,
    )


@pytest.fixture()
def lot_factory():
    """``make_lot`` som fixture, så testfiler ikke skal importere fra conftest."""
    return make_lot


@pytest.fixture()
def sample_db(tmp_path) -> str:
    """Database med aktive, netop udløbne og gamle lots — samt ikke-fund.

    Bemærk at ``a4`` og ``a5`` aldrig markeres som notificeret. De er
    arkivets 'resten', som søgningen skal kunne finde.
    """
    path = tmp_path / "hunter.db"
    with Store(path) as store:
        run_id = store.start_run()
        store.record_lots([
            make_lot("a1", "Sennheiser HD650 forstærker og højttaler",
                     ends_in_hours=3, image="https://auktionshuset.dk/i/1.jpg",
                     first_bid=400, total=620),
            make_lot("a2", "Synology DS1817+ NAS med 8 harddiske",
                     ends_in_hours=72, first_bid=3000, total=4400),
            make_lot("a3", "Dell PowerEdge R720 rack-server",
                     ends_in_hours=-5, first_bid=800, total=1100,
                     auction_id="a2", auction_title="Auktion Køge"),
            make_lot("a4", "Div. havemøbler i teak",
                     ends_in_hours=24, first_bid=200, total=280),
            make_lot("a5", "Gammel pladespiller Thorens TD160",
                     ends_in_hours=48, first_bid=900, total=1250),
            make_lot("a6", "Quad 405 effektforstærker",
                     ends_in_hours=-70, first_bid=1500, total=2050),
        ])
        for lot_id, category, cost in [
            ("a1", "audio_hifi", 620),
            ("a2", "it_tech", 4400),
            ("a3", "it_tech", 1100),
            ("a6", "audio_hifi", 2050),
        ]:
            store.mark_notified(lot_id, category, cost)
        store.finish_run(run_id, auctions=2, lots=6, matches=4, new_matches=4)
        store.enqueue_review(
            "h1", lot_id="a5", category_key="audio_hifi",
            title="Thorens TD160", url="https://x/5", reason="Stand ukendt",
        )
    return str(path)


@pytest.fixture()
def sample_config(tmp_path) -> str:
    """En kopi af den rigtige interests.yml, sikker at redigere i."""
    target = tmp_path / "interests.yml"
    shutil.copy(REPO_ROOT / "config" / "interests.yml", target)
    return str(target)
