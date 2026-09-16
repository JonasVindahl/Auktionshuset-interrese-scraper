"""Indlæsning af interesseprofil med miljøvariabel-overrides.

Prioritet: miljøvariabler > YAML-fil > indbyggede standardværdier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .fees import DEFAULT_OPENING_BID
from .textmatch import distinct_forms, find_keywords

DEFAULT_CONFIG_PATH = Path("config/interests.yml")

# Deres auktionsvilkår tillader højst ét scrape hvert 15. minut.
# Værdien er bevidst ikke konfigurerbar — se MIN_SCRAPE_INTERVAL_SECONDS.
MIN_SCRAPE_INTERVAL_SECONDS = 900


class ConfigError(RuntimeError):
    """Konfigurationen kunne ikke læses eller valideres."""


@dataclass(frozen=True)
class Category:
    """En interessekategori med nøgleord i tre styrker.

    Opdelingen afspejler hvad et ord alene kan bære af betydning:

    ``strong``  produkttyper som 'højttaler' og 'espressomaskine'. Et enkelt
                træf er nok — ordet er en konkret vare brugeren vil have.
    ``weak``    brede eller flertydige ord som 'server' og 'stereo'. Kræver
                mindst to *forskellige* træffere, ellers drukner signalet.
    ``brands``  mærkenavne som 'sennheiser' og 'synology'. Et mærke alene siger
                intet: 'Div. batterier SENNHEISER' og 'Flightcase SENNHEISER' er
                ikke HiFi. Et mærke kræver derfor en produkttype ved siden af.

    ``exact``   nøgleord der kun må matche som selvstændigt ord. Bruges til
                mærker der også er orddele — 'mission' står i 'transmission'.
    """

    key: str
    label: str
    emoji: str
    max_price: int
    strong: tuple[str, ...] = ()
    weak: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    exact: tuple[str, ...] = ()

    def match(self, text: str) -> tuple[bool, tuple[str, ...]]:
        """Returnér (er_match, hvilke nøgleord der udløste det)."""
        strong_hits = find_keywords(text, self.strong, exact=self.exact)
        if strong_hits:
            return True, strong_hits

        weak_hits = find_keywords(text, self.weak, exact=self.exact)
        brand_hits = find_keywords(text, self.brands, exact=self.exact)

        # Mærke + produkttype. 'Sennheiser forstærker' er et signal;
        # 'Flightcase Sennheiser' er det ikke, fordi flightcase ikke er et
        # nøgleord i kategorien.
        if brand_hits and weak_hits:
            return True, brand_hits + weak_hits

        if len(distinct_forms(weak_hits)) >= 2:
            return True, weak_hits
        return False, ()


@dataclass(frozen=True)
class Source:
    base_url: str = "https://auktionshuset.dk"
    region_ids: tuple[str, ...] = ("lyr0boj4d8",)
    region_label: str = "Sjælland"
    auction_status: int = 1


@dataclass(frozen=True)
class ClassifierConfig:
    """Indstillinger for AI-trinnet, læst fra 'classifier' i interests.yml.

    Hemmeligheden (API-nøglen) læses ikke her, men af ``secrets.get_secret``, så
    den kan komme fra miljø, fil eller indirekte. Alt andet kan stå i YAML-filen
    og versionsfølges i git sammen med nøgleordene.
    """

    enabled: bool = False
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    max_per_run: int = 25
    timeout: int = 30
    profile: str = ""

    @property
    def configured(self) -> bool:
        """Om trinnet er slået til og har en profil at arbejde ud fra."""
        return bool(self.enabled and self.profile.strip())


@dataclass(frozen=True)
class Config:
    source: Source
    categories: tuple[Category, ...]
    max_price: int
    soft_over_budget_factor: float
    regions: dict[str, str] = field(default_factory=dict)
    exclude: tuple[str, ...] = ()
    opening_bid: int = DEFAULT_OPENING_BID
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)

    def category(self, key: str) -> Category | None:
        return next((c for c in self.categories if c.key == key), None)

    def is_excluded(self, text: str) -> tuple[str, ...]:
        """Nøgleord der eksplicit gør et lot uinteressant.

        Slår altid igennem, uanset hvilken kategori der ellers ville matche.
        """
        return find_keywords(text, self.exclude)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} skal være et heltal, fik {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} skal være et tal, fik {raw!r}") from exc


def _env_list(name: str) -> tuple[str, ...] | None:
    """Kommasepareret liste, fx REGION_IDS=abc,def."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _normalize_keywords(values: Any) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, str):
        values = [values]
    return tuple(str(v).strip().lower() for v in values if str(v).strip())


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Konfigurationsfilen findes ikke: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ugyldig YAML i {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} skal indeholde et YAML-mapping i toppen")

    source_raw = raw.get("source") or {}
    base_url = os.environ.get("BASE_URL") or source_raw.get(
        "base_url", "https://auktionshuset.dk"
    )

    region_ids = _env_list("REGION_IDS")
    if region_ids is None:
        region_ids = tuple(str(r) for r in (source_raw.get("region_ids") or ["lyr0boj4d8"]))
    if not region_ids:
        raise ConfigError("Mindst ét region-id skal være angivet (REGION_IDS)")

    source = Source(
        base_url=base_url.rstrip("/"),
        region_ids=region_ids,
        region_label=str(source_raw.get("region_label", "Sjælland")),
        auction_status=_env_int("AUCTION_STATUS", int(source_raw.get("auction_status", 1))),
    )

    budget_raw = raw.get("budget") or {}
    max_price = _env_int("MAX_PRICE", int(budget_raw.get("max_price", 1000)))
    soft_factor = _env_float(
        "SOFT_OVER_BUDGET_FACTOR", float(budget_raw.get("soft_over_budget_factor", 2.5))
    )

    categories_raw = raw.get("categories") or {}
    if not categories_raw:
        raise ConfigError("Ingen kategorier fundet under 'categories'")

    categories: list[Category] = []
    for key, spec in categories_raw.items():
        spec = spec or {}
        categories.append(
            Category(
                key=str(key),
                label=str(spec.get("label", key)),
                emoji=str(spec.get("emoji", "")),
                max_price=_env_int(f"MAX_PRICE_{str(key).upper()}", int(spec.get("max_price", max_price))),
                strong=_normalize_keywords(spec.get("strong")),
                weak=_normalize_keywords(spec.get("weak")),
                brands=_normalize_keywords(spec.get("brands")),
                exact=_normalize_keywords(spec.get("exact")),
            )
        )

    return Config(
        source=source,
        categories=tuple(categories),
        max_price=max_price,
        soft_over_budget_factor=soft_factor,
        regions={str(k): str(v) for k, v in (raw.get("regions") or {}).items()},
        exclude=_normalize_keywords(raw.get("exclude")),
        opening_bid=_env_int("OPENING_BID", int(raw.get("opening_bid", DEFAULT_OPENING_BID))),
        classifier=_load_classifier(raw.get("classifier") or {}),
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "ja", "yes", "on")


def _load_classifier(spec: dict[str, Any]) -> ClassifierConfig:
    """Læs AI-indstillingerne. API-nøglen hentes ikke her — se secrets.py."""
    profile = spec.get("profile") or ""
    return ClassifierConfig(
        enabled=_env_bool("CLASSIFIER_ENABLED", _env_bool("LLM_ENABLED", bool(spec.get("enabled", False)))),
        model=os.environ.get("CLASSIFIER_MODEL") or str(spec.get("model", "gpt-4o-mini")),
        base_url=(os.environ.get("CLASSIFIER_BASE_URL") or str(
            spec.get("base_url", "https://api.openai.com/v1")
        )).rstrip("/"),
        max_per_run=_env_int(
            "CLASSIFIER_MAX_PER_RUN", int(spec.get("max_per_run", 25))
        ),
        timeout=_env_int("CLASSIFIER_TIMEOUT", int(spec.get("timeout", 30))),
        profile=str(profile),
    )
