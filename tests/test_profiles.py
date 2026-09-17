"""Profiler: navngivne interessesaet ovenpaa den samme konfiguration.

En profil maa kunne indsnaevre kategorierne, saette sit eget prisloft, have sine
egne udelukkelser og slaaes fra. Uden 'profiles' i YAML'en skal alt opfoere sig
praecis som foer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auction_hunter.config import ConfigError, Profile, load_config
from auction_hunter.matcher import match_lot

YAML = """budget:
  max_price: 500
  soft_over_budget_factor: 1.0

categories:
  it_tech:
    label: IT
    strong: [server]
  audio_hifi:
    label: Audio
    strong: [forstaerker]

exclude: [have]

profiles:
  it:
    label: IT og netvaerk
    categories: [it_tech]
    max_price: 1000
  lyd:
    label: Lyd
    categories: [audio_hifi]
    exclude: [thorens]
  alt:
    label: Alt
    categories: null
  slukket:
    label: Slukket
    categories: [it_tech]
    enabled: false
"""

UDPROFIL = """budget:
  max_price: 500
categories:
  it_tech:
    label: IT
    strong: [server]
profiles:
  it:
    categories: [findes_ikke]
"""


def _config(tmp_path: Path, body: str = YAML):
    path = tmp_path / "interests.yml"
    path.write_text(body, encoding="utf-8")
    return load_config(path)


@pytest.fixture(autouse=True)
def _rene_profilmiljoer(monkeypatch):
    monkeypatch.delenv("PROFILE_IT_ENABLED", raising=False)
    monkeypatch.delenv("PROFILE_SLUKKET_ENABLED", raising=False)


def _lot(lot_factory, title, bid=100):
    return lot_factory("L1", title, first_bid=bid, total=bid)


# -- indlaesning -----------------------------------------------------------

def test_uden_profiler_er_der_en_implicit_standard(tmp_path):
    path = tmp_path / "interests.yml"
    path.write_text(
        "budget:\n  max_price: 500\ncategories:\n  it_tech:\n    strong: [server]\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.profiles == ()
    assert [p.key for p in config.active_profiles()] == ["standard"]


def test_profiler_laeses_med_kategorier_og_loft(tmp_path):
    config = _config(tmp_path)
    it = config.profile("it")
    assert it is not None
    assert it.label == "IT og netvaerk"
    assert it.categories == ("it_tech",)
    assert it.max_price == 1000


def test_alt_betyder_alle_kategorier(tmp_path):
    config = _config(tmp_path)
    alt = config.profile("alt")
    assert alt is not None
    assert config.profile_categories(alt) == config.categories


def test_slukket_profil_er_ikke_aktiv(tmp_path):
    config = _config(tmp_path)
    assert "slukket" not in [p.key for p in config.active_profiles()]


def test_profil_kan_taendes_med_miljoevariabel(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILE_SLUKKET_ENABLED", "1")
    config = _config(tmp_path)
    assert "slukket" in [p.key for p in config.active_profiles()]


def test_ukendt_kategori_giver_configfejl(tmp_path):
    with pytest.raises(ConfigError):
        _config(tmp_path, UDPROFIL)


# -- matchning -------------------------------------------------------------

def test_uden_profil_er_matchningen_uaendret(tmp_path, lot_factory):
    config = _config(tmp_path)
    lot = _lot(lot_factory, "Dell server")
    assert len(match_lot(lot, config)) == len(
        match_lot(lot, config, profile=config.default_profile())
    )


def test_profil_indsnaevrer_kategorierne(tmp_path, lot_factory):
    config = _config(tmp_path)
    lot = _lot(lot_factory, "Sennheiser forstaerker")
    it = config.profile("it")
    assert match_lot(lot, config, profile=it) == []
    lyd = config.profile("lyd")
    assert [m.category.key for m in match_lot(lot, config, profile=lyd)] == ["audio_hifi"]


def test_profilens_udelukkelse_gaelder_kun_den(tmp_path, lot_factory):
    config = _config(tmp_path)
    lot = _lot(lot_factory, "Thorens forstaerker")
    lyd = config.profile("lyd")
    assert match_lot(lot, config, profile=lyd) == []
    # Uden profilen er der ingen udelukkelse af 'thorens'.
    assert match_lot(lot, config)


def test_global_udelukkelse_gaelder_ogsaa_for_profil(tmp_path, lot_factory):
    config = _config(tmp_path)
    lot = _lot(lot_factory, "Server i have")
    it = config.profile("it")
    assert match_lot(lot, config, profile=it) == []


def test_profilens_loft_kan_stramme(tmp_path, lot_factory):
    config = _config(tmp_path)
    lot = _lot(lot_factory, "Dell server", bid=5000)
    it = config.profile("it")
    assert match_lot(lot, config, profile=it) == []


def test_profilens_loft_kan_haeeves(tmp_path, lot_factory):
    config = _config(tmp_path)
    # Salær og moms ganger buddet med ca. 1,9, saa 400 kr lander mellem
    # topniveauets 500 og profilens 1000.
    lot = _lot(lot_factory, "Dell server", bid=400)
    assert match_lot(lot, config) == []
    it = config.profile("it")  # max_price 1000
    assert match_lot(lot, config, profile=it)


# -- koersel og notifikationer ---------------------------------------------

def test_match_profiles_giver_et_fund_pr_profil(tmp_path, lot_factory):
    from auction_hunter.runner import _match_profiles

    config = _config(tmp_path)
    keys = sorted(
        m.profile_key for m in _match_profiles([_lot(lot_factory, "Dell server")], config)
    )
    # 'it' og 'alt' matcher; 'lyd' har kun lydkategorien, og 'slukket' er slukket.
    assert keys == ["alt", "it"]


def test_dedupe_by_lot_holder_et_fund_pr_lot(tmp_path, lot_factory):
    from auction_hunter.runner import _dedupe_by_lot, _match_profiles

    config = _config(tmp_path)
    matches = _match_profiles([_lot(lot_factory, "Dell server")], config)
    assert len(matches) == 2
    assert len(_dedupe_by_lot(matches)) == 1


def test_notifier_for_profile_uden_webhook_bruger_faelles():
    from auction_hunter.notifier import DiscordNotifier
    from auction_hunter.runner import _notifier_for_profile

    default = DiscordNotifier("https://discord.com/api/webhooks/1/x")
    assert _notifier_for_profile(None, default) is default
    assert _notifier_for_profile(Profile(key="a", label="A"), default) is default


def test_notifier_for_profile_uden_hemmelighed_giver_none(monkeypatch):
    from auction_hunter.notifier import DiscordNotifier
    from auction_hunter.runner import _notifier_for_profile

    monkeypatch.delenv("MIN_WEBHOOK", raising=False)
    monkeypatch.delenv("MIN_WEBHOOK_FILE", raising=False)
    default = DiscordNotifier("https://discord.com/api/webhooks/1/x")
    profile = Profile(key="a", label="A", webhook_env="MIN_WEBHOOK")
    assert _notifier_for_profile(profile, default) is None


def test_notify_profiles_markerer_pr_profil(tmp_path, lot_factory):
    from auction_hunter.runner import _match_profiles, _notify_profiles
    from auction_hunter.storage import Store

    config = _config(tmp_path)
    matches = _match_profiles([_lot(lot_factory, "Dell server")], config)

    sent_groups: list[list[str]] = []

    class FakeNotifier:
        def send_matches(self, group, *, heading, details):
            sent_groups.append(sorted(m.profile_key for m in group))
            return type("R", (), {"sent": list(group)})()

    with Store(":memory:") as store:
        total = _notify_profiles(config, FakeNotifier(), matches, {}, "Test", store)
        assert total == 2
        assert sorted(sent_groups) == [["alt"], ["it"]]
        # Nu er begge profiler markeret, og intet er nyt laengere.
        assert store.filter_new(matches) == []


def test_samme_lot_kan_give_en_besked_pr_profil(tmp_path, lot_factory):
    from auction_hunter.storage import Store

    config = _config(tmp_path)
    lot = _lot(lot_factory, "Dell server")
    it = config.profile("it")
    alt = config.profile("alt")

    with Store(":memory:") as store:
        first = match_lot(lot, config, profile=it)
        store.mark_notified(
            lot.lot_id, first[0].category.key, first[0].cost, first[0].profile_key
        )
        # Profilen 'alt' har ikke fået sin besked endnu.
        assert store.filter_new(match_lot(lot, config, profile=alt))


# -- migration -------------------------------------------------------------

def test_gamle_notifikationer_faar_standardprofilen(tmp_path):
    import sqlite3

    from auction_hunter.storage import Store

    path = tmp_path / "gammel.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE notifications (
            lot_id       TEXT NOT NULL,
            category_key TEXT NOT NULL,
            sent_at      TEXT NOT NULL,
            cost         INTEGER,
            PRIMARY KEY (lot_id, category_key)
        );
        """
    )
    conn.execute(
        "INSERT INTO notifications (lot_id, category_key, sent_at, cost)"
        " VALUES ('L1','it_tech','2026-01-01T00:00:00',100)"
    )
    conn.commit()
    conn.close()

    with Store(path) as store:
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(notifications)")}
        assert "profile_key" in columns
        assert store.already_notified("L1", "it_tech")
        assert store.already_notified("L1", "it_tech", "standard")
        assert not store.already_notified("L1", "it_tech", "hifi")
        count = store.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        assert count == 1

