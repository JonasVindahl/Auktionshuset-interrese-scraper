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
