"""Evalueringsværktøj: kør matcheren mod de gemte lots og vis resultatet.

Bruges til at måle effekten af ændringer i interests.yml i stedet for at gætte.
Køres med:  PYTHONPATH=src .venv/bin/python tools/evaluate.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "src")

from auction_hunter.config import load_config  # noqa: E402
from auction_hunter.matcher import match_all, sort_matches  # noqa: E402
from auction_hunter.scraper import Lot  # noqa: E402

FIXTURE = Path("tests/fixtures/lots_sample.json")


def _to_lot(row: dict) -> Lot:
    """Byg et Lot ud af en raekke, uanset om den kommer fra 'export' eller ej.

    ``auction_hunter export`` dumper hukommelsen, og dens 'lots'-tabel hedder
    noget andet end scraperens felter: den har last_bid og last_total, ikke
    current_bid og total_price, og slet ingen has_bids. Begge navne accepteres,
    saa den dokumenterede vej faktisk virker.
    """
    bid = row.get("current_bid", row.get("last_bid"))
    total = row.get("total_price", row.get("last_total"))
    return Lot(
        lot_id=row["lot_id"], title=row.get("title", ""), url=row.get("url", ""),
        lot_number=row.get("lot_number", ""), auction_id=row.get("auction_id", ""),
        auction_title=row.get("auction_title", ""),
        current_bid=bid, total_price=total, ends_at=None, image_url="",
        has_bids=row.get("has_bids", bool(bid)),
    )


def load_lots() -> list[Lot]:
    if not FIXTURE.exists():
        raise SystemExit(
            f"Fandt ikke {FIXTURE}.\n\n"
            "Filen er et snapshot af rigtige lots og er bevidst ikke i git, fordi\n"
            "auktionerne ændrer sig. Lav dit eget snapshot med:\n\n"
            f"    PYTHONPATH=src .venv/bin/python -m auction_hunter export --out {FIXTURE}\n\n"
            "Testene i tests/test_corpus.py kører uden snapshottet, da de bruger\n"
            "fastlagte titler."
        )
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # 'export' skriver et objekt med én noegle pr. tabel; en raa liste af lots
    # accepteres ogsaa, saa et haandlavet snapshot stadig virker.
    rows = data.get("lots", []) if isinstance(data, dict) else data
    if not rows:
        raise SystemExit(
            f"{FIXTURE} indeholder ingen lots. Har agenten koert mindst én gang?"
        )
    return [_to_lot(r) for r in rows]


def main() -> int:
    config = load_config("config/interests.yml")
    lots = load_lots()
    matches = sort_matches(match_all(lots, config))

    print(f"Fund: {len(matches)} af {len(lots)} lots "
          f"({len(matches) / len(lots) * 100:.1f} %)\n")

    print("Pr. kategori:")
    for key, count in Counter(m.category.key for m in matches).most_common():
        print(f"  {key:<22} {count}")

    print("\nNoegleord der udloeser flest fund (stoej kan ses her):")
    keyword_counts: Counter[str] = Counter()
    for m in matches:
        keyword_counts.update(m.keywords)
    for keyword, count in keyword_counts.most_common(15):
        marker = "  <-- meget bredt" if count >= 5 else ""
        print(f"  {count:>3}  {keyword}{marker}")

    print(f"\n{'Pris':>8}  {'Kategori':<20}  Titel")
    print("-" * 104)
    for m in matches:
        over = " [OVER]" if m.over_budget else ""
        kind = "est" if m.is_estimate else "bud"
        print(f"{m.cost:>6} {kind}  {m.category.label[:18]:<20}  "
              f"{m.lot.title[:50]}{over}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
