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
class DetailsConfig:
    """Om lot-siderne skal hentes.

    Slået fra som standard: det er et ekstra kald til auktionshuset pr. lot, og
    vilkårene tillader kun ét katalog-scrape hvert 15. minut. Den der slår det
    til, bør have læst vilkårene og holde loftet lavt.
    """

    enabled: bool = False
    max_per_run: int = 10
    pause_seconds: float = 2.0


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
    details: DetailsConfig = field(default_factory=DetailsConfig)

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


# Genveje der betyder "alle landsdele i regions-mappingen".
_ALL_REGION_ALIASES = {"all", "alle", "*", "danmark", "hele danmark"}


def _resolve_region_ids(
    env_value: tuple[str, ...] | None,
    source_raw: dict[str, Any],
    regions_map: dict[str, str],
) -> tuple[str, ...]:
    """Region-id'er fra miljø eller YAML, med 'all' som genvej.

    Miljøet vinder over YAML, som resten af konfigurationen. 'all' udvides til
    alle id'er i regions-mappingen, så man ikke skal vedligeholde listen to
    steder når auktionshuset tilføjer en landsdel.
    """
    if env_value is not None:
        values: tuple[str, ...] = env_value
    else:
        yaml_value = source_raw.get("region_ids")
        if isinstance(yaml_value, str):
            values = (yaml_value,)
        elif yaml_value:
            values = tuple(str(v) for v in yaml_value)
        else:
            values = ("lyr0boj4d8",)

    if any(str(v).strip().lower() in _ALL_REGION_ALIASES for v in values):
        if not regions_map:
            raise ConfigError(
                "region_ids beder om alle landsdele, men 'regions' er tom i konfigurationen"
            )
        return tuple(regions_map.values())
    return tuple(str(v).strip() for v in values if str(v).strip())


def _region_label(
    source_raw: dict[str, Any],
    regions_map: dict[str, str],
    region_ids: tuple[str, ...],
) -> str:
    """Et læsbart navn på de valgte regioner.

    Sættes eksplicit med REGION_LABEL eller region_label. Ellers udledes navnet
    fra regions-mappingen, så teksten ikke påstår "Sjælland" når man følger
    hele landet.
    """
    explicit = os.environ.get("REGION_LABEL") or source_raw.get("region_label")
    if explicit:
        return str(explicit)
    id_to_name = {v: k for k, v in regions_map.items()}
    if regions_map and set(region_ids) == set(regions_map.values()):
        return "Hele Danmark"
    names = [id_to_name.get(rid, rid) for rid in region_ids]
    return ", ".join(names)


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

    regions_map = {str(k): str(v) for k, v in (raw.get("regions") or {}).items()}

    source_raw = raw.get("source") or {}
    base_url = os.environ.get("BASE_URL") or source_raw.get(
        "base_url", "https://auktionshuset.dk"
    )

    region_ids = _resolve_region_ids(_env_list("REGION_IDS"), source_raw, regions_map)
    if not region_ids:
        raise ConfigError("Mindst ét region-id skal være angivet (REGION_IDS)")

    source = Source(
        base_url=base_url.rstrip("/"),
        region_ids=region_ids,
        region_label=_region_label(source_raw, regions_map, region_ids),
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
        regions=regions_map,
        exclude=_normalize_keywords(raw.get("exclude")),
        opening_bid=_env_int("OPENING_BID", int(raw.get("opening_bid", DEFAULT_OPENING_BID))),
        classifier=_load_classifier(raw.get("classifier") or {}),
        details=_load_details(raw.get("details") or {}),
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "ja", "yes", "on")


def _load_details(spec: dict[str, Any]) -> DetailsConfig:
    """Læs indstillingerne for lot-siderne. Slået fra som standard."""
    return DetailsConfig(
        enabled=_env_bool("DETAILS_ENABLED", bool(spec.get("enabled", False))),
        max_per_run=_env_int("DETAILS_MAX_PER_RUN", int(spec.get("max_per_run", 10))),
        pause_seconds=float(
            os.environ.get("DETAILS_PAUSE_SECONDS") or spec.get("pause_seconds", 2.0)
        ),
    )


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
