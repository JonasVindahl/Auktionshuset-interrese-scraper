"""Tests for de regelbaserede profilforslag.

Forslagene bygger paa feedback, saa testene laver deres egen lille historik:
nogle lots der blev sendt og afvist, nogle der blev koebt uden at matche, og
nogle hvor andelen af afvisninger er for lav til at sige noget.
"""

from __future__ import annotations

from auction_hunter.config import Category, Config, Source
from auction_hunter.storage import Store
from auction_hunter.web import suggest


def _config(*, strong=("switch",)) -> Config:
    return Config(
        source=Source(),
        categories=(
            Category(key="it_tech", label="IT / tech", emoji="", max_price=1000, strong=strong),
        ),
        max_price=1000,
        soft_over_budget_factor=2.5,
    )


def _notify(store, lot_id, *, category="it_tech", action=None):
    store.mark_notified(lot_id, category, 125)
    if action:
        store.conn.execute(
            "INSERT INTO feedback (lot_id, category_key, action, title, created_at)"
            " VALUES (?,?,?,'x','2026-09-16T00:00:00+00:00')",
            (lot_id, category, action),
        )


def test_foreslaar_at_fjerne_et_stoejende_noegleord(tmp_path, lot_factory):
    with Store(tmp_path / "t.db") as store:
        for i in range(6):
            store.record_lot(lot_factory(f"l{i}", f"Switch model {i}"), 125)
            _notify(store, f"l{i}", action="skip" if i < 5 else None)
        store.conn.commit()
        out = suggest.noisy_keywords(store.conn, _config())

    assert len(out) == 1
    assert out[0].keyword == "switch"
    assert out[0].level == "strong"
    assert out[0].seen == 6 and out[0].skip == 5


def test_under_taersklen_giver_ingen_forslag(tmp_path, lot_factory):
    with Store(tmp_path / "t.db") as store:
        for i in range(4):
            store.record_lot(lot_factory(f"l{i}", f"Switch model {i}"), 125)
            _notify(store, f"l{i}", action="skip")
        store.conn.commit()
        assert suggest.noisy_keywords(store.conn, _config()) == []


def test_mange_afvisninger_men_lav_andel_giver_ingen_forslag(tmp_path, lot_factory):
    with Store(tmp_path / "t.db") as store:
        for i in range(10):
            store.record_lot(lot_factory(f"l{i}", f"Switch model {i}"), 125)
            _notify(store, f"l{i}", action="skip" if i < 4 else "bought")
        store.conn.commit()
        assert suggest.noisy_keywords(store.conn, _config()) == []


def test_finder_kob_profilen_ikke_fangede(tmp_path, lot_factory):
    with Store(tmp_path / "t.db") as store:
        store.record_lot(lot_factory("b1", "Sennheiser HD650 forstaerker"), 500)
        store.conn.execute(
            "INSERT INTO feedback (lot_id, category_key, action, title, created_at)"
            " VALUES ('b1','audio_hifi','bought','x','2026-09-16T00:00:00+00:00')"
        )
        store.conn.commit()
        out = suggest.unmatched_marks(store.conn, _config())

    assert [row["lot_id"] for row in out] == ["b1"]


def test_annotate_impact_markerer_brud_paa_facitlisten():
    from auction_hunter.web.suggest import Suggestion, annotate_impact

    forslag = Suggestion(action="remove", keyword="switch", category="it_tech",
                         level="strong", seen=6, skip=5, pct=83)
    out = annotate_impact([forslag], _config(), [
        {"title": "Switch TP-LINK", "expect": "yes", "why": "x"},
    ])
    assert out[0].checked is True
    assert out[0].breaks == ("Switch TP-LINK",)


def test_annotate_impact_uden_facitliste_er_ukontrolleret():
    from auction_hunter.web.suggest import Suggestion, annotate_impact

    forslag = Suggestion(action="remove", keyword="switch", category="it_tech",
                         level="strong", seen=6, skip=5, pct=83)
    out = annotate_impact([forslag], _config(), [])
    assert out[0].checked is False
    assert out[0].breaks == ()


# -- modellens forslag -----------------------------------------------------

def test_from_model_godtager_et_gyldigt_forslag():
    from auction_hunter.web.suggest import from_model

    out = from_model(
        [{"noegleord": "synology", "kategori": "it_tech", "niveau": "brands",
          "grund": "mærkenavn"}],
        _config(), ["Synology DS1817+ NAS"],
    )
    assert len(out) == 1
    assert out[0].action == "add" and out[0].source == "model"
    assert out[0].level == "brands"


def test_from_model_afviser_det_den_ikke_kan_efterproeve():
    from auction_hunter.web.suggest import from_model

    titles = ["Synology DS1817+ NAS"]
    forslag = [
        {"noegleord": "synology", "kategori": "findes-ikke", "niveau": "brands"},
        {"noegleord": "synology", "kategori": "it_tech", "niveau": "top"},
        {"noegleord": "cisco", "kategori": "it_tech", "niveau": "brands"},
        {"noegleord": "", "kategori": "it_tech", "niveau": "brands"},
    ]
    assert from_model(forslag, _config(), titles) == []


def test_from_model_springer_ord_der_allerede_staar_over():
    from auction_hunter.web.suggest import from_model

    out = from_model(
        [{"noegleord": "switch", "kategori": "it_tech", "niveau": "strong"}],
        _config(), ["Switch TP-LINK TL-SG1016D"],
    )
    assert out == []


def test_suggest_keywords_laeser_modellens_json():
    from auction_hunter.classifier import OpenAICompatibleClient
    from auction_hunter.web import chat

    def transport(*, url, headers, payload, timeout):
        return 200, {"choices": [{"message": {"content":
            '{"forslag": [{"titel": "x", "noegleord": "synology", '
            '"kategori": "it_tech", "niveau": "brands", "grund": "mærke"}]}'}}]}

    client = OpenAICompatibleClient(
        base_url="https://x/v1", api_key="k", model="m", transport=transport
    )
    entries = chat.suggest_keywords(_config(), client, ["Synology DS1817+ NAS"])
    assert len(entries) == 1 and entries[0]["noegleord"] == "synology"


def test_suggest_keywords_ved_fejl_giver_tomt():
    from auction_hunter.classifier import OpenAICompatibleClient
    from auction_hunter.web import chat

    def transport(**kwargs):
        raise RuntimeError("nede")

    client = OpenAICompatibleClient(
        base_url="https://x/v1", api_key="k", model="m", transport=transport
    )
    assert chat.suggest_keywords(_config(), client, ["Synology DS1817+ NAS"]) == []
