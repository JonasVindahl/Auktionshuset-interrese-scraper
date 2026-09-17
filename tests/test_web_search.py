"""Tests for arkivsøgningen.

Arkivet er alt hvad agenten har set — også de lots der aldrig blev til et fund.
Det er hele pointen med siden, så testene her holder især øje med at
ikke-fund faktisk kan findes.
"""

from __future__ import annotations

import pytest

from auction_hunter.storage import Store
from auction_hunter.web import queries
from auction_hunter.web.search import (
    SearchQuery, archive_stats, categories_seen, search, to_fts_query,
)


@pytest.fixture()
def conn(sample_db):
    connection = queries.ro_conn(sample_db)
    yield connection
    connection.close()


# -- FTS-udtryk ------------------------------------------------------------

def test_fts_udtryk_citerer_ord():
    assert to_fts_query("sennheiser") == '("sennheiser"*)'


def test_fts_udtryk_soeger_baade_raat_og_normaliseret():
    result = to_fts_query("højttaler")
    assert '"højttaler"*' in result
    assert '"hoejttaler"*' in result


def test_fts_udtryk_kombinerer_ord_med_and():
    assert " AND " in to_fts_query("sennheiser forstærker")


def test_fts_udtryk_neutraliserer_citationstegn():
    """Et citationstegn ville ellers bryde ud af udtrykket og give syntaksfejl."""
    result = to_fts_query('nas" OR "1"="1')
    assert result.count('"') % 2 == 0


def test_fts_udtryk_paa_tom_tekst():
    assert to_fts_query("   ") == ""


# -- søgning ---------------------------------------------------------------

def test_finder_lot_der_aldrig_blev_et_fund(conn):
    """'havemøbler' rammer ingen kategori, men skal stadig kunne findes."""
    result = search(conn, SearchQuery(text="havemøbler"))
    assert result.total == 1
    assert "havemøbler" in result.rows[0]["title"].lower()
    assert result.rows[0]["was_match"] == 0


def test_finder_lot_med_foldede_tegn(conn):
    """'hojttaler' skal finde 'højttaler'."""
    assert search(conn, SearchQuery(text="hojttaler")).total >= 1


def test_praefiks_match(conn):
    assert search(conn, SearchQuery(text="pladespil")).total >= 1


def test_filter_kun_ikke_fund(conn):
    result = search(conn, SearchQuery(matched="kun_ikke_fund"))
    assert result.total >= 1
    assert all(row["was_match"] == 0 for row in result.rows)


def test_filter_kun_fund(conn):
    result = search(conn, SearchQuery(matched="kun_fund"))
    assert result.total >= 1
    assert all(row["was_match"] == 1 for row in result.rows)


def test_prisfilter_nedre_graense(conn):
    # Filtret bruger samme pris som kortet viser: totalen inkl. salær og moms.
    result = search(conn, SearchQuery(min_price=1000))
    assert all(
        (row["cost"] or row["last_total"] or row["last_bid"] or 0) >= 1000
        for row in result.rows
    )


def test_prisfilter_oevre_graense(conn):
    result = search(conn, SearchQuery(max_price=500))
    assert all(
        (row["last_total"] or row["last_bid"] or 0) <= 500 for row in result.rows
    )


def test_statusfilter_aktive(conn):
    """ends_at kan ikke sammenlignes i SQL — filtreringen sker i Python."""
    result = search(conn, SearchQuery(status="aktive"))
    titles = {row["title"] for row in result.rows}
    assert any("Sennheiser" in t for t in titles)
    assert not any("Dell PowerEdge" in t for t in titles)


def test_statusfilter_afsluttede(conn):
    result = search(conn, SearchQuery(status="afsluttede"))
    titles = {row["title"] for row in result.rows}
    assert any("Dell PowerEdge" in t for t in titles)
    assert not any("Sennheiser" in t for t in titles)


def test_kategorifilter(conn):
    result = search(conn, SearchQuery(category="it_tech"))
    assert result.total >= 1
    assert all(row["category_key"] == "it_tech" for row in result.rows)


def test_sortering_efter_pris(conn):
    result = search(conn, SearchQuery(sort="pris_ned"))
    prices = [(r["cost"] or r["last_total"] or r["last_bid"] or 0) for r in result.rows]
    assert prices == sorted(prices, reverse=True)


def test_tom_soegning_giver_alt(conn):
    result = search(conn, SearchQuery())
    assert result.total == 6


def test_soegning_uden_traeffere(conn):
    assert search(conn, SearchQuery(text="findesheltsikkertikke")).total == 0


def test_ugyldige_vaerdier_falder_tilbage_til_standard():
    query = SearchQuery(status="drop table", matched="???", sort="; --").normalized()
    assert query.status == "alle"
    assert query.matched == "alle"
    assert query.sort == "relevans"


def test_negativ_side_bliver_til_foerste_side():
    assert SearchQuery(page=-5).normalized().page == 1


# -- paginering ------------------------------------------------------------

def test_paginering(tmp_path, lot_factory):
    path = tmp_path / "mange.db"
    with Store(path) as store:
        run_id = store.start_run()
        store.record_lots([
            lot_factory(f"p{i}", f"Testlot nummer {i}", first_bid=i, total=i)
            for i in range(120)
        ])
        store.finish_run(run_id)

    connection = queries.ro_conn(str(path))
    try:
        first = search(connection, SearchQuery(text="testlot", page=1))
        assert first.total == 120
        assert len(first.rows) == 50
        assert first.pages == 3
        assert first.has_next and not first.has_prev

        last = search(connection, SearchQuery(text="testlot", page=3))
        assert len(last.rows) == 20
        assert last.has_prev and not last.has_next

        # En side ud over den sidste klemmes ned til den sidste.
        assert search(connection, SearchQuery(text="testlot", page=99)).page == 3
    finally:
        connection.close()


# -- indeks og statistik ---------------------------------------------------

def test_arkivstatistik(conn):
    stats = archive_stats(conn)
    assert stats["total"] == 6
    assert stats["matched"] == 4
    assert stats["unmatched"] == 2
    assert stats["indexed"] == 6


def test_kategorier_i_brug(conn):
    assert set(categories_seen(conn)) == {"audio_hifi", "it_tech"}


def test_indeks_opdateres_naar_titlen_aendrer_sig(tmp_path, lot_factory):
    path = tmp_path / "titel.db"
    with Store(path) as store:
        store.record_lot(lot_factory("t1", "Oprindelig titel"), 100)
        store.record_lot(lot_factory("t1", "Ny og anderledes titel"), 100)

    connection = queries.ro_conn(str(path))
    try:
        assert search(connection, SearchQuery(text="anderledes")).total == 1
        assert search(connection, SearchQuery(text="oprindelig")).total == 0
        # Ingen dubletter i indekset.
        assert archive_stats(connection)["indexed"] == 1
    finally:
        connection.close()


def test_indeks_bygges_for_gammel_database(tmp_path, lot_factory):
    """En database fra før søgningen skal indekseres ved næste opstart."""
    import sqlite3

    path = tmp_path / "gammel.db"
    with Store(path) as store:
        store.record_lots([lot_factory("g1", "Et gammelt lot med harddiske")])

    # Simulér en database uden indeks.
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE lots_fts")
    raw.commit()
    raw.close()

    with Store(path):
        pass  # åbningen bygger indekset igen

    connection = queries.ro_conn(str(path))
    try:
        assert search(connection, SearchQuery(text="harddiske")).total == 1
    finally:
        connection.close()


def test_kategori_og_ikke_fund_kan_ikke_kombineres(tmp_path, lot_factory):
    """Et ikke-fund har ingen kategori, saa de to filtre gav altid nul."""
    with Store(tmp_path / "t.db") as store:
        store.record_lot(
            lot_factory("nf", "Computer LENOVO ThinkCentre M720q", first_bid=325), None
        )
        store.conn.commit()
        query = SearchQuery(
            text="thinkcentre", matched="kun_ikke_fund", category="it_tech"
        ).normalized()
        assert query.category == ""
        assert search(store.conn, query).total == 1


def test_prisfilter_rammer_den_viste_pris(tmp_path, lot_factory):
    """325 i bud er 488 kr inkl. salær og moms, altsaa det kortet viser."""
    with Store(tmp_path / "t.db") as store:
        store.record_lot(
            lot_factory("nf", "Computer LENOVO ThinkCentre M720q", first_bid=325), None
        )
        store.conn.commit()
        assert search(store.conn, SearchQuery(min_price=487, max_price=489)).total == 1
