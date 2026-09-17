"""Konfiguration af regioner.

Regionerne er det eneste sted hvor agentens rækkevidde bestemmes, så de er
testet for sig: 'all' skal udvides fra regions-mappingen, og etiketten skal
følge med, så beskederne ikke påstår "Sjælland" når man følger hele landet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auction_hunter.config import ConfigError, load_config

BASE = """source:
  base_url: https://auktionshuset.dk
{extra}
regions:
  Nordjylland: nord
  Midtjylland: midt
  Sjaelland: sjae

categories:
  it:
    label: IT
    strong: [server]
"""


def _write(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "interests.yml"
    path.write_text(BASE.format(extra=extra), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _rene_regionmiljoer(monkeypatch):
    monkeypatch.delenv("REGION_IDS", raising=False)
    monkeypatch.delenv("REGION_LABEL", raising=False)


def test_all_udvides_til_alle_landsdele(tmp_path):
    config = load_config(_write(tmp_path, "  region_ids: all"))
    assert config.source.region_ids == ("nord", "midt", "sjae")
    assert config.source.region_label == "Hele Danmark"


def test_streng_all_uden_bindestreg(tmp_path):
    config = load_config(_write(tmp_path, "  region_ids: 'all'"))
    assert config.source.region_ids == ("nord", "midt", "sjae")


def test_enkelt_region_faar_navn_fra_mappingen(tmp_path):
    config = load_config(_write(tmp_path, "  region_ids:\n    - nord"))
    assert config.source.region_ids == ("nord",)
    assert config.source.region_label == "Nordjylland"


def test_flere_regioner_saettes_sammen_til_en_etikette(tmp_path):
    config = load_config(_write(tmp_path, "  region_ids:\n    - nord\n    - sjae"))
    assert config.source.region_label == "Nordjylland, Sjaelland"


def test_eksplicit_label_vinner_over_udledt(tmp_path):
    config = load_config(
        _write(tmp_path, "  region_ids: all\n  region_label: Mit omraade")
    )
    assert config.source.region_label == "Mit omraade"


def test_env_region_ids_vinner_over_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("REGION_IDS", "sjae")
    config = load_config(_write(tmp_path, "  region_ids: all"))
    assert config.source.region_ids == ("sjae",)


def test_env_region_label_vinner(tmp_path, monkeypatch):
    monkeypatch.setenv("REGION_IDS", "all")
    monkeypatch.setenv("REGION_LABEL", "Fra miljoeet")
    config = load_config(_write(tmp_path, "  region_ids: all"))
    assert config.source.region_label == "Fra miljoeet"


def test_all_uden_mapping_giver_configfejl(tmp_path):
    path = tmp_path / "interests.yml"
    path.write_text(
        "source:\n  region_ids: all\n\ncategories:\n  it:\n    strong: [server]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)
