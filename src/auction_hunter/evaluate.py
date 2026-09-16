"""Kør interesseprofilen mod facitlisten uden at skrive noget.

Bruges både af facitliste-testen og af dashboardet, når et foreslået nøgleord
skal vurderes før det anvendes. Facitlisten bor i config/ sammen med profilen,
så den også er med i Docker-imaget, hvor kun src/ og config/ kopieres ind.

'yes' og 'no' er hårde krav. 'maybe' er grænsetilfælde og blokerer intet.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from .config import Config
from .matcher import match_lot
from .scraper import Lot

DEFAULT_CORPUS = Path("config/match_expectations.jsonl")
HARD = ("yes", "no")
LEVELS = ("strong", "weak", "brands", "exact")


def corpus_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("CORPUS_PATH")
    return Path(env) if env else DEFAULT_CORPUS


def load_corpus(path: str | Path | None = None) -> list[dict]:
    """Facitlisten, eller en tom liste hvis den ikke findes her."""
    resolved = corpus_path(path)
    if not resolved.is_file():
        return []
    lines = resolved.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _lot(title: str) -> Lot:
    return Lot(
        lot_id="corpus", title=title,
        url="https://auktionshuset.dk/auktioner/test/lots/1/x",
        lot_number="1", auction_id="A1", auction_title="Testauktion",
        current_bid=100, total_price=188, ends_at=None, image_url="", has_bids=True,
    )


def matches(config: Config, title: str) -> bool:
    return bool(match_lot(_lot(title), config))


def hard_failures(config: Config, cases: list[dict]) -> dict[str, str]:
    """Titler hvor profilen svarer forkert på et hårdt krav."""
    out: dict[str, str] = {}
    for case in cases:
        expect = case.get("expect")
        if expect not in HARD:
            continue
        title = str(case.get("title") or "")
        hit = matches(config, title)
        if (expect == "yes" and not hit) or (expect == "no" and hit):
            out[title] = str(case.get("why") or "")
    return out


def apply_edit(
    config: Config, *, action: str, category: str, level: str, keyword: str
) -> Config | None:
    """En kopi af konfigurationen med én ændring, eller None hvis den er ugyldig."""
    keyword = keyword.strip().lower()
    level = level.strip().lower()
    if not keyword or level not in LEVELS or action not in ("add", "remove"):
        return None

    categories = []
    found = False
    for cat in config.categories:
        if cat.key != category:
            categories.append(cat)
            continue
        found = True
        current = getattr(cat, level)
        if action == "remove":
            updated = tuple(k for k in current if k != keyword)
        else:
            updated = current if keyword in current else current + (keyword,)
        categories.append(replace(cat, **{level: updated}))

    return replace(config, categories=tuple(categories)) if found else None


def corpus_impact(
    config: Config,
    cases: list[dict],
    *,
    action: str,
    category: str,
    level: str,
    keyword: str,
) -> dict | None:
    """Hvad en ændring ville gøre ved facitlisten.

    None når facitlisten ikke er tilgængelig eller ændringen er ugyldig, så
    kalderen kan se forskel på ingen effekt og kunne ikke tjekkes.
    """
    if not cases:
        return None
    after = apply_edit(config, action=action, category=category, level=level, keyword=keyword)
    if after is None:
        return None
    before = set(hard_failures(config, cases))
    now = set(hard_failures(after, cases))
    return {"breaks": sorted(now - before), "fixes": sorted(before - now)}
