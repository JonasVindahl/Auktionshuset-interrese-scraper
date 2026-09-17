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
    _fallback_filter,
    ask,
    build_filter,
    explain_title,
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


def test_tomt_soegeresultat_giver_ikke_fremmede_lots(conn):
    """Ord der ikke findes maa ikke erstattes af alt muligt andet.

    Det var den fejl der fik et spoergsmaal om et server rack til at blive
    besvaret med en switch.
    """
    client = FakeClient(
        json.dumps({"soegeord": ["findes-slet-ikke-xyz"], "kategori": "it_tech"}),
        json.dumps({"valgte": [], "svar": "Intet."}),
    )
    answer = ask(conn, client, "noget med it", categories=["it_tech"])
    assert answer.total == 0
    assert answer.rows == []
    assert "matcher" in answer.text.lower()
    # Filteret viser det der faktisk blev soegt paa, ikke et oprydnet et.
    assert "findes-slet-ikke-xyz" in answer.archive_url


def test_and_loeses_til_or_naar_det_giver_nul(conn):
    """Et for stramt AND-filter maa ikke give et tomt svar."""
    client = FakeClient(
        json.dumps({"soegeord": ["sennheiser", "findes-slet-ikke"],
                    "alle_ord": True}),
        json.dumps({"valgte": [1], "svar": "Her er en Sennheiser."}),
    )
    answer = ask(conn, client, "sennheiser", categories=["it_tech"])
    assert answer.rows
    assert "hver for sig" in answer.text


def test_soegeord_renses_for_citationstegn():
    client = FakeClient(json.dumps({"soegeord": ['"server rack"', "rackmonteret"]}))
    query, _ = build_filter(client, "server rack")
    assert '"' not in query.text
    assert "server rack" in query.text
    assert "rackmonteret" in query.text


def test_kategorien_loesnes_naar_den_giver_nul(tmp_path, lot_factory):
    from auction_hunter.storage import Store

    with Store(tmp_path / "t.db") as store:
        store.record_lot(
            lot_factory("nf", "Computer LENOVO ThinkCentre M720q", first_bid=325), None
        )
        store.conn.commit()
        client = FakeClient(
            json.dumps({"soegeord": ["thinkcentre"], "kategori": "it_tech"}),
            json.dumps({"valgte": [1], "svar": "Her er den."}),
        )
        answer = ask(store.conn, client, "find thinkcentre", categories=["it_tech"])

    assert answer.rows
    assert "hele arkivet" in answer.text


def test_filteret_viser_kategorien(conn):
    client = FakeClient(
        json.dumps({"kategori": "it_tech"}),
        json.dumps({"valgte": [1], "svar": "Svar."}),
    )
    answer = ask(
        conn, client, "it?", categories=["it_tech"], labels={"it_tech": "IT / tech"}
    )
    assert answer.filter_used.get("kategori") == "IT / tech"


def test_rangordning_uden_stigninger(conn):
    client = FakeClient(json.dumps({"liste": "stigere"}))
    answer = ask(conn, client, "hvad er steget mest i pris?")
    assert answer.rows == []
    assert "steget" in answer.text.lower()


def test_ask_svarer_paa_prisstigninger(tmp_path, lot_factory):
    from auction_hunter.storage import Store

    with Store(tmp_path / "t.db") as store:
        store.record_lots(
            [
                lot_factory("a", "Stille lot", first_bid=100, total=125),
                lot_factory("b", "Stiger lot", first_bid=100, total=125),
            ],
            {"a": 125, "b": 125},
        )
        # b faar et hoejere bud, saa den er steget siden foerste registrering.
        store.record_lots(
            [lot_factory("b", "Stiger lot", first_bid=300, total=375)], {"b": 375}
        )
        store.conn.commit()

        client = FakeClient(json.dumps({"liste": "stigere"}))
        answer = ask(store.conn, client, "hvad er gået mest op i pris?")

    assert answer.rows
    assert answer.rows[0]["lot_id"] == "b"
    assert "Stiger" in answer.text


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


def test_sammenlign_giver_en_vurdering(conn):
    client = FakeClient(
        json.dumps({"kategori": "it_tech"}),
        json.dumps({"valgte": [1], "svar": "Svar.", "sammenlign": 1}),
    )
    answer = ask(conn, client, "hvad er den vaerd?", categories=["it_tech"])
    assert answer.valuation is not None
    assert answer.valuation.title
    assert answer.valuation.lot_id == answer.rows[0]["lot_id"]


# -- loft over dashboardets AI-forbrug --------------------------------------

class TestAIBudget:
    """Agenten har altid haft max_per_run. Dashboardet havde intet loft."""

    @pytest.fixture(autouse=True)
    def _frisk_taeller(self):
        from auction_hunter.web import aibudget

        aibudget.BUDGET.reset()
        yield
        aibudget.BUDGET.reset()

    def test_kald_taelles_ned(self, monkeypatch):
        from auction_hunter.web import aibudget

        monkeypatch.setenv("WEB_AI_MAX_PER_HOUR", "3")
        klient = aibudget.BudgetedClient(
            base_url="http://x", api_key="k", model="m",
            transport=lambda **kw: (200, {"choices": [{"message": {"content": "hej"}}]}),
        )
        assert aibudget.BUDGET.remaining() == 3
        klient.complete(system="s", user="u")
        klient.complete(system="s", user="u")
        assert aibudget.BUDGET.remaining() == 1
        assert aibudget.BUDGET.allow()
        klient.complete(system="s", user="u")
        assert not aibudget.BUDGET.allow()

    def test_loft_paa_nul_slaar_ai_helt_fra(self, monkeypatch):
        from auction_hunter.web import aibudget

        monkeypatch.setenv("WEB_AI_MAX_PER_HOUR", "0")
        assert not aibudget.BUDGET.allow()
        assert aibudget.status()["exhausted"] is True

    def test_brugt_loft_giver_ingen_klient_og_vaelter_ikke_siden(
        self, sample_db, sample_config, monkeypatch
    ):
        """Et brugt loft skal opfoere sig som en manglende noegle, ikke som en fejl."""
        from fastapi.testclient import TestClient

        from auction_hunter.web import aibudget
        from auction_hunter.web import app as app_mod

        monkeypatch.setenv("DB_PATH", sample_db)
        monkeypatch.setenv("CONFIG_PATH", sample_config)
        monkeypatch.setenv("CLASSIFIER_API_KEY", "sk-test")
        monkeypatch.setenv("WEB_AI_MAX_PER_HOUR", "1")
        monkeypatch.delenv("WEB_PASSWORD", raising=False)

        assert app_mod.llm_client() is not None
        aibudget.BUDGET.record()
        assert app_mod.llm_client() is None, "loftet skulle have lukket for klienten"

        client = TestClient(app_mod.create_app())
        assert client.get("/chat").status_code == 200
        assert client.get("/interests", params={"ai": "1"}).status_code == 200

    def test_drift_viser_forbruget(self, sample_db, sample_config, monkeypatch):
        from fastapi.testclient import TestClient

        from auction_hunter.web import app as app_mod

        monkeypatch.setenv("DB_PATH", sample_db)
        monkeypatch.setenv("CONFIG_PATH", sample_config)
        monkeypatch.setenv("WEB_AI_MAX_PER_HOUR", "42")
        monkeypatch.delenv("WEB_PASSWORD", raising=False)
        body = TestClient(app_mod.create_app()).get("/drift").text
        assert "AI-forbrug fra dashboardet" in body
        assert "42" in body


# -- gemte vurderinger i skabelonen ----------------------------------------

class TestGemtVurderingRenderes:
    """/chat gav 500 saa snart en samtale indeholdt sammenlignelige salg.

    Assistentens svar gemmes som JSON, saa objektet paa vej tilbage er en
    almindelig dict. Feltet hed 'items', og Jinja finder dict.items foer
    noeglen af samme navn, saa '{% for sale in comps.items %}' itererede over
    en bundet metode.

    Testene daekkede kun den levende vej, hvor comps er en Comparables med
    .items som rigtigt attribut. Selve JSON-rundturen blev aldrig renderet, og
    det er praecis der fejlen laa.
    """

    def _gem_samtale(self, db, comps_noegle="sales"):
        from auction_hunter.web import chatstore

        store = chatstore.ChatStore(chatstore.chat_db_path(db))
        try:
            cid = store.new_conversation(title="hvad er den vaerd")
            store.append(cid, "user", "hvad er en HD650 vaerd?")
            store.append(cid, "assistant", "Typisk omkring 900 kr.", {
                "valuation": {
                    "lot_id": "a1", "title": "Sennheiser HD650",
                    "url": "http://x/1", "total": 900, "series": [500, 900],
                    "comps": {
                        "count": 1, "median": 900, "low": 700, "high": 1100,
                        comps_noegle: [{
                            "lot_id": "a6", "title": "Sennheiser HD650 sortering",
                            "hammer": 700, "total": 900,
                            "ended_at": "2026-01-02T00:00:00", "shared": ["hd650"],
                        }],
                    },
                },
            })
            return cid
        finally:
            store.close()

    def _client(self, sample_db, sample_config, monkeypatch):
        from fastapi.testclient import TestClient

        from auction_hunter.web.app import create_app

        monkeypatch.setenv("DB_PATH", sample_db)
        monkeypatch.setenv("CONFIG_PATH", sample_config)
        monkeypatch.delenv("WEB_PASSWORD", raising=False)
        monkeypatch.delenv("CLASSIFIER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        return TestClient(create_app(), raise_server_exceptions=False)

    def test_vurdering_med_sammenlignelige_salg_renderes(
        self, sample_db, sample_config, monkeypatch
    ):
        cid = self._gem_samtale(sample_db)
        svar = self._client(sample_db, sample_config, monkeypatch).get(f"/chat?c={cid}")

        assert svar.status_code == 200
        # Ikke bare 200: panelet skal faktisk indeholde salget. En tom
        # gennemloebning ville ogsaa give 200 og skjule at listen er vaek.
        assert "Hvad samme slags er gået for" in svar.text
        assert "Sennheiser HD650 sortering" in svar.text

    def test_samtaler_gemt_under_det_gamle_navn_virker_stadig(
        self, sample_db, sample_config, monkeypatch
    ):
        """Feltet hed 'items' indtil kollisionen. De ligger der i 90 dage."""
        cid = self._gem_samtale(sample_db, comps_noegle="items")
        svar = self._client(sample_db, sample_config, monkeypatch).get(f"/chat?c={cid}")

        assert svar.status_code == 200
        assert "Sennheiser HD650 sortering" in svar.text
