"""Tests for webdashboardet.

Modulet havde ingen dækning, og det kostede: ``row.get()`` på en
``sqlite3.Row`` findes ikke, så forsiden crashede først i drift. Testene her
rammer derfor de steder hvor rækker læses, hvor tid sammenlignes, og hvor
databasen kan være ældre end koden.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from auction_hunter import web
from auction_hunter.scraper import Lot
from auction_hunter.storage import Store

UTC = timezone.utc


def _lot(lot_id: str, title: str, *, ends_in_hours: float | None, image: str = "",
         first_bid: int = 100, total: int = 150) -> Lot:
    ends = (
        datetime.now(UTC) + timedelta(hours=ends_in_hours)
        if ends_in_hours is not None else None
    )
    return Lot(
        lot_id=lot_id, title=title, url=f"https://auktionshuset.dk/lots/{lot_id}",
        lot_number=lot_id[-1], auction_id="a1", auction_title="Testauktion",
        current_bid=first_bid, total_price=total, ends_at=ends,
        image_url=image, has_bids=True,
    )


@pytest.fixture()
def db(tmp_path):
    """En database med et aktivt, et netop udløbet og et gammelt udløbet lot."""
    path = tmp_path / "test.db"
    with Store(path) as store:
        run_id = store.start_run()
        store.record_lots([
            _lot("act1", "Sennheiser forstærker", ends_in_hours=3,
                 image="https://auktionshuset.dk/img/1.jpg", first_bid=400, total=620),
            _lot("act2", "Synology NAS", ends_in_hours=72, first_bid=3000, total=4400),
            _lot("exp1", "Dell rack-server", ends_in_hours=-5, first_bid=800, total=1100),
            _lot("old1", "Quad forstærker", ends_in_hours=-70, first_bid=1500, total=2050),
            _lot("noend", "Lot uden sluttidspunkt", ends_in_hours=None),
        ])
        for lot_id, cat, cost in [
            ("act1", "hifi", 620), ("act2", "it_tech", 4400),
            ("exp1", "it_tech", 1100), ("old1", "hifi", 2050),
            ("noend", "hifi", 150),
        ]:
            store.mark_notified(lot_id, cat, cost)
        store.finish_run(run_id, auctions=1, lots=5, matches=5, new_matches=5)
    return str(path)


# -- tidsberegning ---------------------------------------------------------

def test_time_left_paa_aktivt_lot():
    ends = (datetime.now(UTC) + timedelta(hours=3)).isoformat(timespec="seconds")
    text, css, secs = web._time_left(ends)
    assert text.startswith("Slutter om")
    assert css == "urgent"          # under 6 timer
    assert 10700 < secs < 10800


def test_time_left_viser_minutter_saa_en_time_ikke_forsvinder():
    """2 t 59 min må ikke vises som '2 t' — det ser ud som en time mindre."""
    ends = (datetime.now(UTC) + timedelta(hours=2, minutes=59)).isoformat()
    text, _, _ = web._time_left(ends)
    assert "2 t" in text and "min" in text


def test_time_left_paa_udloebet_lot():
    ends = (datetime.now(UTC) - timedelta(hours=5)).isoformat(timespec="seconds")
    text, css, secs = web._time_left(ends)
    assert text == "Sluttede 5 t siden"
    assert css == "ended"
    assert secs == 0                # sorterer efter de aktive


def test_time_left_uden_sluttidspunkt():
    assert web._time_left(None) == ("", "", 0)
    assert web._time_left("noget vrøvl") == ("", "", 0)


def test_parse_dt_antager_utc_naar_tidszone_mangler():
    dt = web._parse_dt("2026-09-16T12:00:00")
    assert dt is not None and dt.tzinfo is timezone.utc


def test_parse_dt_bevarer_offset():
    dt = web._parse_dt("2026-09-16T14:30:00+02:00")
    assert dt is not None
    assert dt.utcoffset() == timedelta(hours=2)


# -- opdeling i aktive og udløbne -----------------------------------------

def test_split_by_end_deler_korrekt(db):
    """ends_at gemmes som ISO med offset ('...T14:30:00+02:00').

    SQLites datetime('now') giver '... 12:05:24' med mellemrum. En SQL-
    strengsammenligning mellem de to er forkert, fordi 'T' sorterer efter
    mellemrum — derfor sker opdelingen i Python.
    """
    conn = web._ro_conn(db)
    try:
        active, expired = web._split_by_end(web._notification_rows(conn))
    finally:
        conn.close()

    active_ids = {r["lot_id"] for r in active}
    expired_ids = {r["lot_id"] for r in expired}

    assert active_ids == {"act1", "act2", "noend"}   # uden sluttid regnes som aktiv
    assert expired_ids == {"exp1"}                   # 70 timer er uden for vinduet
    assert "old1" not in expired_ids


def test_split_by_end_sorterer_udloebne_nyeste_foerst(tmp_path):
    path = tmp_path / "s.db"
    with Store(path) as store:
        run_id = store.start_run()
        store.record_lots([
            _lot("e1", "Sluttede for lidt siden", ends_in_hours=-2),
            _lot("e2", "Sluttede for laenge siden", ends_in_hours=-40),
        ])
        store.mark_notified("e1", "hifi", 100)
        store.mark_notified("e2", "hifi", 100)
        store.finish_run(run_id)

    conn = web._ro_conn(str(path))
    try:
        _, expired = web._split_by_end(web._notification_rows(conn))
    finally:
        conn.close()
    assert [r["lot_id"] for r in expired] == ["e1", "e2"]


# -- rendering -------------------------------------------------------------

def test_forsiden_viser_kun_aktive(db):
    html = web._render_main(db).decode()
    assert "Sennheiser" in html
    assert "Synology" in html
    assert "Dell rack-server" not in html


def test_expired_viser_kun_lots_i_vinduet(db):
    html = web._render_expired(db).decode()
    assert "Dell rack-server" in html
    assert "Quad" not in html          # 70 timer, uden for 48-timers-vinduet
    assert "Sennheiser" not in html


def test_forsiden_rammer_ikke_row_get(db):
    """sqlite3.Row har ingen .get() — regressionen der crashede forsiden."""
    html = web._render_main(db).decode()
    assert "Auktionshuset Hunter" in html
    assert len(html) > 1000


def test_billede_vises_naar_det_findes(db):
    html = web._render_main(db).decode()
    assert "auktionshuset.dk/img/1.jpg" in html


def test_manglende_billede_giver_pladsholder(db):
    html = web._render_main(db).decode()
    assert "thumb-empty" in html       # Synology-lot'et har intet billede


def test_prisstigning_vises(db):
    """Et bud der er steget siden første observation skal fremgå."""
    with Store(db) as store:
        lot = _lot("act1", "Sennheiser forstærker", ends_in_hours=3,
                   image="https://auktionshuset.dk/img/1.jpg", first_bid=900, total=1200)
        store.record_lot(lot, 1200)
    html = web._render_main(db).decode()
    assert "price-rise" in html


def test_kontroller_er_med_paa_forsiden(db):
    html = web._render_main(db).decode()
    for needle in ("data-sort='ending'", "ending-chip", "price-min", "price-max",
                   "data-period='today'", "reset-btn"):
        assert needle in html, needle


def test_expired_har_ingen_periodefilter(db):
    """Alle lots på siden ligger per definition i 48-timers-vinduet."""
    html = web._render_expired(db).decode()
    assert "data-period=" not in html
    assert "price-min" in html         # men prisfilter giver stadig mening


def test_html_escapes_i_titler(tmp_path):
    path = tmp_path / "x.db"
    with Store(path) as store:
        run_id = store.start_run()
        store.record_lots([_lot("x1", "<script>alert(1)</script>", ends_in_hours=2)])
        store.mark_notified("x1", "hifi", 100)
        store.finish_run(run_id)
    html = web._render_main(str(path)).decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_tom_database_giver_stadig_en_side(tmp_path):
    path = tmp_path / "tom.db"
    with Store(path):
        pass
    html = web._render_main(str(path)).decode()
    assert "Auktionshuset Hunter" in html
    assert "Ingen aktive fund" in html


# -- feedback --------------------------------------------------------------

def test_feedback_gemmes_og_vises(db):
    web._save_feedback(db, "act1", "hifi", "bought", "Sennheiser")
    assert "data-feedback='bought'" in web._render_main(db).decode()


def test_feedback_fjernes_ved_tom_handling(db):
    web._save_feedback(db, "act1", "hifi", "bought", "Sennheiser")
    web._save_feedback(db, "act1", "hifi", "", "")
    assert "data-feedback='bought'" not in web._render_main(db).decode()


def test_feedback_overskriver_tidligere_markering(db):
    web._save_feedback(db, "act1", "hifi", "bid", "Sennheiser")
    web._save_feedback(db, "act1", "hifi", "bought", "Sennheiser")
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT action FROM feedback WHERE lot_id='act1'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("bought",)]


# -- migration -------------------------------------------------------------

def test_migration_tilfoejer_image_url_til_gammel_database(tmp_path):
    """En database fra før billed-understøttelsen skal kunne åbnes."""
    path = tmp_path / "gammel.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE lots (
            lot_id TEXT PRIMARY KEY, auction_id TEXT NOT NULL,
            auction_title TEXT DEFAULT '', title TEXT DEFAULT '',
            url TEXT DEFAULT '', lot_number TEXT DEFAULT '',
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            first_bid INTEGER, last_bid INTEGER, last_total INTEGER, ends_at TEXT);
        INSERT INTO lots VALUES ('g1','a','Auktion','Gammelt lot','https://x/1','1',
            '2026-09-01T10:00:00+00:00','2026-09-01T10:00:00+00:00',100,150,200,NULL);
        """
    )
    conn.commit()
    conn.close()

    with Store(path):
        pass  # åbningen kører migrationen

    conn = sqlite3.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(lots)")}
        row = conn.execute("SELECT title, last_bid FROM lots").fetchone()
    finally:
        conn.close()

    assert "image_url" in columns
    assert row == ("Gammelt lot", 150), "migrationen må ikke røre eksisterende data"


def test_migration_er_idempotent(tmp_path):
    path = tmp_path / "to-gange.db"
    with Store(path):
        pass
    with Store(path):
        pass  # må ikke fejle på 'duplicate column name'


def test_billede_bevares_naar_et_udtraek_mangler_det(tmp_path):
    """Lazy loading kan give et tomt billedfelt i én kørsel uden at det er væk."""
    path = tmp_path / "img.db"
    with Store(path) as store:
        store.record_lot(_lot("i1", "Lot", ends_in_hours=5,
                              image="https://auktionshuset.dk/img/9.jpg"), 100)
        store.record_lot(_lot("i1", "Lot", ends_in_hours=5, image=""), 100)
        row = store.conn.execute(
            "SELECT image_url FROM lots WHERE lot_id='i1'"
        ).fetchone()
    assert row["image_url"] == "https://auktionshuset.dk/img/9.jpg"


# -- HTTP ------------------------------------------------------------------

@pytest.fixture()
def server(db):
    web._Handler.db_path = db
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode()


def _post(url: str, payload: dict) -> int:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status


def test_http_forside(server):
    status, body = _get(server + "/")
    assert status == 200
    assert "Auktionshuset Hunter" in body


def test_http_expired(server):
    status, body = _get(server + "/expired")
    assert status == 200
    assert "Dell rack-server" in body


def test_http_ukendt_sti_giver_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server + "/findes-ikke")
    assert exc.value.code == 404


def test_http_feedback_gemmer(server):
    assert _post(server + "/feedback", {
        "lot_id": "act1", "category_key": "hifi",
        "action": "bought", "title": "Sennheiser",
    }) == 200
    assert "data-feedback='bought'" in _get(server + "/")[1]


def test_http_feedback_afviser_ukendt_handling(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server + "/feedback", {
            "lot_id": "act1", "category_key": "hifi", "action": "drop table",
        })
    assert exc.value.code == 400


def test_http_feedback_afviser_manglende_lot_id(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server + "/feedback", {"category_key": "hifi", "action": "skip"})
    assert exc.value.code == 400
