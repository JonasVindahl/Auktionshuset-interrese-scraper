"""Profiler: navngivne interessesaet ovenpaa den samme konfiguration.

En profil maa kunne indsnaevre kategorierne, saette sit eget prisloft, have sine
egne udelukkelser og slaaes fra. Uden 'profiles' i YAML'en skal alt opfoere sig
praecis som foer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auction_hunter.config import ConfigError, load_config
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
