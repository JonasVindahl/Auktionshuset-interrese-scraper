"""Tests for chat-assistenten.

To ting testene vogter:

* Modellen skriver aldrig SQL. Den leverer et struktureret filter, og Python
  bygger forespørgslen — så en prompt-injektion i en lot-titel kan ikke nå
  databasen.
* Assistenten svarer også når modellen ikke kan nås. Fail-open er samme
  princip som i klassificeringen: et teknisk problem må ikke gøre værktøjet
  ubrugeligt.
"""

from __future__ import annotations

import json

import pytest

from auction_hunter.classifier import LLMError
from auction_hunter.config import load_config
from auction_hunter.web import queries
from auction_hunter.web.chat import (
    ask, build_filter, explain_title, _fallback_filter,
)


class FakeClient:
    """Sprogmodel der svarer med det den får besked på."""

    def __init__(self, *responses: str, fail: bool = False) -> None:
        self.responses = list(responses)
        self.fail = fail
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, max_tokens: int = 120) -> str:
        self.calls.append({"system": system, "user": user})
        if self.fail:
            raise LLMError("modellen kunne ikke nås")
        return self.responses.pop(0) if self.responses else "{}"


@pytest.fixture()
def conn(sample_db):
    connection = queries.ro_conn(sample_db)
    yield connection
    connection.close()


# -- filterbygning ---------------------------------------------------------

def test_modellens_filter_bruges():
    client = FakeClient(json.dumps({"text": "sennheiser", "max_price": 1000}))
    query, degraded = build_filter(client, "har der været Sennheiser under 1000?")
    assert query.text == "sennheiser"
    assert query.max_price == 1000
    assert degraded is False


def test_filter_i_kodeblok_parses():
    client = FakeClient('```json\n{"text": "nas"}\n```')
    query, _degraded = build_filter(client, "nas?")
    assert query.text == "nas"


def test_ugyldigt_modelsvar_falder_tilbage():
    client = FakeClient("det her er ikke JSON overhovedet")
    query, degraded = build_filter(client, "har der været Sennheiser?")
    assert degraded is True
    assert "sennheiser" in query.text


def test_modelfejl_falder_tilbage():
    client = FakeClient(fail=True)
    query, degraded = build_filter(client, "har der været nogen NAS?")
    assert degraded is True
    assert "nas" in query.text


def test_uden_klient_falder_tilbage():
    query, degraded = build_filter(None, "har der været Sennheiser?")
    assert degraded is True
    assert "sennheiser" in query.text


def test_ugyldige_vaerdier_fra_modellen_saniteres():
    """Modellen kan finde på hvad som helst — det skal ikke nå databasen."""
    client = FakeClient(json.dumps({
        "text": "nas", "status": "DROP TABLE lots", "sort": "; --",
        "max_price": "ikke et tal", "matched": "???",
    }))
    query, _degraded = build_filter(client, "nas?")
    assert query.status == "alle"
    assert query.sort == "relevans"
    assert query.max_price is None
    assert query.matched == "alle"


def test_tomt_filter_bliver_til_noegleordssoegning():
    client = FakeClient("{}")
    query, _degraded = build_filter(client, "har der været nogen pladespillere?")
    assert "pladespillere" in query.text


# -- fallback --------------------------------------------------------------

def test_fallback_fjerner_stopord():
    query = _fallback_filter("har der været nogen Sennheiser forstærkere?")
    assert "sennheiser" in query.text
    assert "har" not in query.text.split()
    assert "der" not in query.text.split()


def test_fallback_paa_tomt_spoergsmaal():
    assert _fallback_filter("hvad er der?").text == ""


# -- ask -------------------------------------------------------------------

def test_ask_uden_klient_svarer_med_resultater(conn):
    answer = ask(conn, None, "har der været nogen Sennheiser?")
    assert answer.degraded is True
    assert "CLASSIFIER_API_KEY" in answer.text
    assert len(answer.rows) >= 1


def test_ask_med_klient_bruger_modellens_svar(conn):
    client = FakeClient(
        json.dumps({"text": "sennheiser"}),
        "Ja, der har været én Sennheiser-forstærker til 620 kr.",
    )
    answer = ask(conn, client, "har der været Sennheiser?")
    assert "620" in answer.text
    assert len(answer.rows) >= 1


def test_ask_naar_modellen_fejler_paa_svaret(conn):
    """Filteret lykkes, men selve svaret fejler — resultaterne skal stadig med."""
    class HalfBroken(FakeClient):
        def complete(self, *, system, user, max_tokens=120):
            if "JSON-filter" in system:
                return json.dumps({"text": "sennheiser"})
            raise LLMError("timeout")

    answer = ask(conn, HalfBroken(), "har der været Sennheiser?")
    assert answer.degraded is True
    assert len(answer.rows) >= 1


def test_ask_paa_tomt_spoergsmaal(conn):
    assert "Stil et spørgsmål" in ask(conn, None, "   ").text


def test_ask_finder_lots_der_aldrig_blev_fund(conn):
    """Hele pointen med arkivet: også det der ikke ramte profilen."""
    client = FakeClient(json.dumps({"text": "havemøbler", "matched": "kun_ikke_fund"}),
                        "Der har været havemøbler i teak.")
    answer = ask(conn, client, "har der været havemøbler?")
    assert any("havemøbler" in row["title"].lower() for row in answer.rows)


def test_ask_sender_ikke_raa_sql_til_modellen(conn):
    """Modellen må aldrig se eller skrive SQL."""
    client = FakeClient(json.dumps({"text": "nas"}), "Svar")
    ask(conn, client, "nas?")
    for call in client.calls:
        assert "SELECT" not in call["system"].upper()
        assert "SELECT" not in call["user"].upper()


# -- forklaring af match ---------------------------------------------------

def test_explain_paa_match(sample_config):
    config = load_config(sample_config)
    trace = explain_title(config, "Proxmark3 RDV4 RFID-læser")
    assert trace.matched is True
    assert "proxmark" in [k.lower() for k in trace.keywords]


def test_explain_paa_udelukket_titel(sample_config):
    config = load_config(sample_config)
    trace = explain_title(config, "Div. tastatur og mus")
    assert trace.matched is False
    assert trace.excluded_by
    assert "udelukket" in trace.as_text().lower()


def test_explain_paa_almindelig_ikke_match(sample_config):
    config = load_config(sample_config)
    trace = explain_title(config, "Havemøbler i teak med hynder")
    assert trace.matched is False
    assert not trace.excluded_by


def test_explain_forklarer_maerke_uden_produkttype(sample_config):
    """Et mærke alene må ikke matche — forklaringen skal sige hvorfor."""
    config = load_config(sample_config)
    trace = explain_title(config, "Div. batterier SENNHEISER")
    if not trace.matched and trace.near_misses:
        assert any("mærke" in m["missing"] for m in trace.near_misses)


def test_explain_bruger_den_rigtige_matcher(sample_config):
    """Forklaringen må ikke kunne afvige fra hvad agenten faktisk gør."""
    from auction_hunter.matcher import match_lot
    from auction_hunter.scraper import Lot

    config = load_config(sample_config)
    title = "Proxmark3 RDV4 RFID-læser"
    lot = Lot(
        lot_id="x", title=title, url="", lot_number="", auction_id="",
        auction_title="", current_bid=None, total_price=None, ends_at=None,
        image_url="", has_bids=False,
    )
    assert explain_title(config, title).matched == bool(
        match_lot(lot, config, opening_bid=config.opening_bid)
    )


# -- modellen vælger selv rækkerne ------------------------------------------

def test_modellen_vaelger_hvilke_lots_der_vises(conn):
    """Søgningen er grov; modellen er det led der kan se om et lot er et svar."""
    client = FakeClient(
        json.dumps({"days_back": 30}),
        json.dumps({"valgte": [1], "svar": "Kun det første lot er relevant."}),
    )
    answer = ask(conn, client, "hvad er relevant?")
    assert answer.selected is True
    assert len(answer.rows) == 1
    assert answer.considered > 1
    assert answer.text == "Kun det første lot er relevant."


def test_ugyldige_numre_ignoreres(conn):
    """Modellen kan svare med hvad som helst; et dårligt nummer må ikke vælte siden."""
    client = FakeClient(
        json.dumps({"text": "sennheiser"}),
        json.dumps({"valgte": [0, 99, "x", None, 1, 1, -1], "svar": "Et lot."}),
    )
    answer = ask(conn, client, "hvad?")
    assert len(answer.rows) == 1
    assert answer.selected is True


def test_ulaeseligt_svar_viser_alt(conn):
    """Kan svaret ikke læses, vises hele søgeresultatet med teksten som den er."""
    client = FakeClient(json.dumps({"text": "sennheiser"}), "Det kan jeg ikke svare på.")
    answer = ask(conn, client, "hvad?")
    assert answer.selected is False
    assert len(answer.rows) == answer.considered
    assert "ikke svare" in answer.text


def test_vis_alle_springer_modellens_valg_over(conn):
    client = FakeClient(
        json.dumps({"days_back": 30}),
        json.dumps({"valgte": [1], "svar": "Et lot."}),
    )
    answer = ask(conn, client, "hvad?", show_all=True)
    assert answer.selected is False
    assert len(answer.rows) == answer.considered
    assert len(answer.rows) > 1


def test_tomt_valg_giver_ingen_raekker(conn):
    client = FakeClient(
        json.dumps({"text": "sennheiser"}),
        json.dumps({"valgte": [], "svar": "Ingen af dem er et svar."}),
    )
    answer = ask(conn, client, "hvad?")
    assert answer.rows == []
    assert answer.selected is True


# -- udvidelse, redning og arkiv-link --------------------------------------

def test_soegeord_udvides_og_slaas_sammen_med_or():
    client = FakeClient(json.dumps({"soegeord": ["nvme", "ssd", "nas", "nuc"]}))
    query, degraded = build_filter(client, "ting der normalt har SSD eller NVMe")
    assert query.any_words is True
    assert query.text == "nvme ssd nas nuc"
    assert degraded is False


def test_alle_ord_kraever_and():
    client = FakeClient(json.dumps({"soegeord": ["sennheiser", "hd650"], "alle_ord": True}))
    query, _ = build_filter(client, "Sennheiser HD650")
    assert query.any_words is False


def test_modellen_kan_vaelge_kategori():
    client = FakeClient(json.dumps({"soegeord": ["nas"], "kategori": "it_tech"}))
    query, _ = build_filter(client, "NAS", categories=["it_tech", "audio_hifi"])
    assert query.category == "it_tech"
    assert "it_tech" in client.calls[0]["user"]


def test_arkivet_link_baerer_filteret():
    from auction_hunter.web.chat import archive_url
    from auction_hunter.web.search import SearchQuery

    url = archive_url(SearchQuery(text="nas nuc", category="it_tech", max_price=2000))
    assert url.startswith("/archive?")
    assert "q=nas+nuc" in url
    assert "category=it_tech" in url
    assert "max_price=2000" in url


def test_tomt_soegeresultat_reddes_af_de_andre_filtre(conn):
    # Modellen vælger et søgeord der ikke findes, men en kategori der gør.
    client = FakeClient(
        json.dumps({"soegeord": ["findes-slet-ikke-xyz"], "kategori": "it_tech"}),
        json.dumps({"valgte": [1], "svar": "Her er hvad der matcher."}),
    )
    answer = ask(conn, client, "noget med it", categories=["it_tech"])
    assert answer.total > 0
    assert answer.rows
    assert "søgeordene" in answer.text
    assert answer.archive_url.startswith("/archive?")


# -- prisoversigt, sammenlignelige salg og markeringer ---------------------

def test_prisoversigt_regner_median():
    from auction_hunter.web.chat import _price_summary

    rows = [
        {"last_total": 100, "last_bid": 80},
        {"last_total": 300, "last_bid": 300},
        {"last_total": None, "last_bid": 200},
    ]
    text = _price_summary(rows)
    assert "median 200" in text
    assert "laveste 100" in text
    assert "3 af 3" in text


def test_prisoversigt_uden_priser_er_tom():
    from auction_hunter.web.chat import _price_summary
    assert _price_summary([{"last_total": None, "last_bid": None}]) == ""


def test_raekker_til_modellen_naevner_markering():
    from auction_hunter.web.chat import _rows_for_model

    rows = [{
        "title": "Synology DS1817+ NAS", "last_total": 4400, "last_bid": 4000,
        "ends_at": None, "was_match": 1, "feedback_action": "skip",
    }]
    assert "du har afvist" in _rows_for_model(rows)


def test_sammenlignelige_er_tomt_naar_intet_ligner(conn):
    from auction_hunter.web.chat import _comparables_note
    assert _comparables_note(conn, []) == ""


def test_svaret_faar_prisoversigt_med(conn):
    client = FakeClient(
        json.dumps({"kategori": "it_tech"}),
        json.dumps({"valgte": [1], "svar": "Svar."}),
    )
    ask(conn, client, "hvad koster de?", categories=["it_tech"])
    assert "median" in client.calls[1]["user"]


def test_sammenlignelige_naevner_priser_og_datoer(tmp_path, lot_factory):
    from auction_hunter.storage import Store
    from auction_hunter.web.chat import _comparables_note

    with Store(tmp_path / "t.db") as store:
        store.record_lots(
            [
                lot_factory("a", "Thorens TD160 pladespiller", ends_in_hours=-720,
                            first_bid=900, total=1250),
                lot_factory("b", "Thorens TD160 pladespiller defekt", ends_in_hours=-48,
                            first_bid=700, total=950),
            ],
            {"a": 1250, "b": 950},
        )
        store.conn.commit()
        note = _comparables_note(
            store.conn, [{"lot_id": "a", "title": "Thorens TD160 pladespiller"}]
        )

    assert "median" in note
    assert "Eksempler" in note
    assert "kr" in note
