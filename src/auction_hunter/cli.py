"""Kommandolinje for Auktionshuset Hunter.

    python -m auction_hunter run          # kør i loop (15 min interval)
    python -m auction_hunter once         # én kørsel
    python -m auction_hunter scan         # vis fund uden at gemme eller sende
    python -m auction_hunter stats        # hvad husker databasen
    python -m auction_hunter export       # dump hukommelsen til JSON
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from .config import MIN_SCRAPE_INTERVAL_SECONDS, ConfigError, load_config
from .matcher import match_all, sort_matches
from .runner import run_forever, run_once, resolve_notifier, setup_logging
from .scraper import ScrapeError, Scraper
from .secrets import SecretError, get_secret, redact
from .storage import Store


def _print_matches(matches, limit: int = 30) -> None:
    if not matches:
        print("Ingen fund.")
        return
    print(f"{'Pris':>8}  {'Type':<4}  {'Kategori':<18}  Titel")
    print("-" * 100)
    for m in matches[:limit]:
        kind = "est." if m.is_estimate else "bud"
        over = " (over loft)" if m.over_budget else ""
        title = m.lot.title[:48]
        print(f"{m.cost:>6} kr  {kind:<4}  {m.category.label[:18]:<18}  {title}{over}")
    if len(matches) > limit:
        print(f"... og {len(matches) - limit} mere")


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    scraper = Scraper(config.source, delay_seconds=args.delay)
    auctions = scraper.fetch_auctions()
    print(f"Fandt {len(auctions)} aktive auktioner i {config.source.region_label}\n")

    lots = []
    for auction in auctions:
        if auction.lot_count == 0:
            continue
        try:
            lots.extend(scraper.fetch_lots(auction))
        except ScrapeError as exc:
            print(f"  ! sprang over: {auction.title[:40]}: {exc}", file=sys.stderr)

    matches = sort_matches(match_all(lots, config, opening_bid=config.opening_bid))
    print(f"\nScanning {len(lots)} lots -> {len(matches)} fund\n")
    _print_matches(matches, limit=args.limit)
    print("\n(scan gemmer intet og sender intet — brug 'once' for rigtig kørsel)")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with Store(args.db) as store:
        notifier = resolve_notifier(dry_run=args.dry_run)
        stats = run_once(config, store, notifier)
    print(
        f"\n{stats.auctions} auktioner, {stats.lots} lots, "
        f"{stats.matches} fund ({stats.new_matches} nye), {stats.notified} sendt"
    )
    if stats.errors:
        print(f"{len(stats.errors)} fejl undervejs:", file=sys.stderr)
        for err in stats.errors:
            print(f"  - {err}", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    run_forever(
        interval_seconds=args.interval,
        config_path=args.config,
        store_path=args.db,
        dry_run=args.dry_run,
    )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        counts = store.counts()
        print("Indhold i databasen:")
        for table, count in counts.items():
            print(f"  {table:<15} {count:>8}")

        verdicts = store.classification_counts()
        if verdicts:
            print("\nAI-vurderinger (cachede):")
            for verdict, count in sorted(verdicts.items()):
                label = {"ja": "interessant", "nej": "afvist", "maaske": "i tvivl"}.get(
                    verdict, verdict
                )
                print(f"  {label:<13} {count:>8}")

        pending = store.pending_review_count()
        if pending:
            print(f"\nTil gennemsyn: {pending} lot(er) venter på næste digest")

        risers = store.price_risers(limit=10)
        if risers:
            print("\nStørste prisstigninger (fra første observation):")
            for row in risers:
                delta = row["last_bid"] - row["first_bid"]
                print(f"  +{delta:>6} kr  {row['title'][:55]}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Vis 'måske'-fundene der endnu ikke er sendt i et digest."""
    with Store(args.db) as store:
        rows = store.pending_reviews(limit=args.limit)
    if not rows:
        print("Ingen lotter til gennemsyn.")
        return 0
    print(f"{len(rows)} lot(er) til gennemsyn:\n")
    for row in rows:
        print(f"  {row['title'][:78]}")
        if row["reason"]:
            print(f"      {row['reason']}")
        if row["url"]:
            print(f"      {row['url']}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        path = store.export_json(args.out)
    print(f"Eksporteret til {path}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Vis hvilke hemmeligheder der er synlige, uden at lække dem."""
    config = load_config(args.config)
    print(f"Region:      {config.source.region_label} ({', '.join(config.source.region_ids)})")
    print(f"Kategorier:  {len(config.categories)}")
    print(f"Budloft:     {config.max_price} kr (blødt x{config.soft_over_budget_factor})")
    print(f"Udelukkelser:{len(config.exclude)} nøgleord")
    print(f"Interval:    min. {MIN_SCRAPE_INTERVAL_SECONDS}s (15 min, låst af auktionsvilkår)")
    print()

    cl = config.classifier
    if not cl.configured:
        print("AI-trin:     slået fra (kører kun på nøgleord)")
        if cl.enabled:
            print("             'enabled' er sat, men 'profile' mangler i interests.yml")
    else:
        try:
            key = get_secret("CLASSIFIER_API_KEY") or get_secret("OPENAI_API_KEY")
        except SecretError as exc:
            print(f"AI-trin:     FEJL — nøglen kunne ikke læses: {exc}", file=sys.stderr)
            return 1
        if key:
            print(f"AI-trin:     aktivt — {cl.model} via {redact(key)}")
        else:
            print("AI-trin:     slået til, men CLASSIFIER_API_KEY mangler — kører uden AI")
    print()

    try:
        webhook = get_secret("DISCORD_WEBHOOK_URL")
        print(f"Discord:     {redact(webhook)}")
        if webhook:
            from .notifier import DiscordNotifier

            ok = DiscordNotifier(webhook).send_text(
                "Auktionshuset Hunter: forbindelsestest gennemført."
            )
            print("Testbesked:  sendt" if ok else "Testbesked:  fejlede")
    except SecretError as exc:
        print(f"Discord:     FEJL — {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data = asdict(config)
    print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    try:
        from .web import serve
    except ImportError as exc:
        print(
            f"Webdashboardet mangler en afhængighed: {exc}\n"
            "Installer dem med: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2
    # Sæt stierne i miljøet, så alle web-moduler ser de samme.
    os.environ["DB_PATH"] = args.db
    if args.config:
        os.environ["CONFIG_PATH"] = args.config
    serve(host=args.host, port=args.port, db_path=args.db)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auction_hunter", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH"), help="sti til interests.yml")
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/auction_hunter.db"))
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))

    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="vis fund uden at gemme eller sende")
    p_scan.add_argument("--limit", type=int, default=30)
    p_scan.add_argument("--delay", type=float, default=1.0, help="sekunder mellem kald")
    p_scan.set_defaults(func=cmd_scan)

    p_once = sub.add_parser("once", help="én rigtig kørsel")
    p_once.add_argument("--dry-run", action="store_true", help="send ikke til Discord")
    p_once.set_defaults(func=cmd_once)

    p_run = sub.add_parser("run", help="kør i loop")
    p_run.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("SCRAPE_INTERVAL_SECONDS", MIN_SCRAPE_INTERVAL_SECONDS)),
        help=f"sekunder mellem kørsler (min. {MIN_SCRAPE_INTERVAL_SECONDS})",
    )
    p_run.add_argument("--dry-run", action="store_true")
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("stats", help="vis databaseindhold").set_defaults(func=cmd_stats)

    p_review = sub.add_parser("review", help="vis 'måske'-fund til gennemsyn")
    p_review.add_argument("--limit", type=int, default=25)
    p_review.set_defaults(func=cmd_review)

    p_export = sub.add_parser("export", help="dump hukommelse til JSON")
    p_export.add_argument("--out", default="reports/export.json")
    p_export.set_defaults(func=cmd_export)

    sub.add_parser("check", help="vis konfiguration og test Discord").set_defaults(func=cmd_check)
    sub.add_parser("dump-config", help="vis fuld effektiv konfiguration").set_defaults(func=cmd_config)

    p_web = sub.add_parser("web", help="start read-only webdashboard")
    p_web.add_argument("--host", default=os.environ.get("WEB_HOST", "0.0.0.0"))
    p_web.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    p_web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    try:
        return args.func(args)
    except (ConfigError, SecretError) as exc:
        print(f"Konfigurationsfejl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
