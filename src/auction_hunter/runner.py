"""Kørsel: scrape -> match -> husk -> notificér.

Intervallet er bevidst ikke konfigurerbart under 15 minutter, fordi
auktionshusets vilkår kræver det. ``MIN_SCRAPE_INTERVAL_SECONDS`` kan derfor
kun overskrides opad.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import MIN_SCRAPE_INTERVAL_SECONDS, Config, ConfigError, load_config
from .matcher import Match, match_all, sort_matches
from .notifier import DiscordNotifier
from .scraper import ScrapeError, Scraper
from .secrets import SecretError, get_secret
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    auctions: int = 0
    lots: int = 0
    matches: int = 0
    new_matches: int = 0
    notified: int = 0
    errors: list[str] = field(default_factory=list)


def run_once(config: Config, store: Store, notifier: DiscordNotifier | None) -> RunStats:
    """Én fuld gennemkørsel."""
    stats = RunStats()
    run_id = store.start_run()
    scraper = Scraper(config.source)

    try:
        auctions = scraper.fetch_auctions()
        stats.auctions = len(auctions)

        all_lots = []
        for auction in auctions:
            if auction.lot_count == 0:
                continue
            try:
                all_lots.extend(scraper.fetch_lots(auction))
            except ScrapeError as exc:
                # Én dårlig auktion må ikke vælte hele kørslen.
                message = f"{auction.title[:40]}: {exc}"
                log.error("Spring over auktion — %s", message)
                stats.errors.append(message)
            scraper._polite_pause()

        stats.lots = len(all_lots)
        store.record_lots(all_lots)

        matches = sort_matches(match_all(all_lots, config, opening_bid=config.opening_bid))
        stats.matches = len(matches)

        new_matches: list[Match] = store.filter_new(matches)
        stats.new_matches = len(new_matches)

        if new_matches and notifier is not None:
            result = notifier.send_matches(
                new_matches,
                heading=f"**{len(new_matches)} nyt fund** i {config.source.region_label}",
            )
            stats.notified = len(result.sent)
            # Kun de fund der faktisk blev leveret markeres, så en fejlet
            # besked forsøges igen ved næste kørsel i stedet for at gå tabt.
            for match in result.sent:
                store.mark_notified(match.lot.lot_id, match.category.key, match.cost)

        store.finish_run(
            run_id,
            auctions=stats.auctions,
            lots=stats.lots,
            matches=stats.matches,
            new_matches=stats.new_matches,
            error="; ".join(stats.errors) or None,
        )
    except Exception as exc:
        store.finish_run(run_id, error=str(exc))
        raise

    return stats


def resolve_notifier(*, dry_run: bool) -> DiscordNotifier | None:
    """Slå Discord-webhooken op, hvis den findes.

    Hemmeligheden opløses ved hver kørsel, så en roteret webhook virker uden
    genstart af containeren.
    """
    if dry_run:
        log.info("Dry-run: sender ikke til Discord")
        return None

    try:
        webhook_url = get_secret("DISCORD_WEBHOOK_URL")
    except SecretError as exc:
        log.error("%s", exc)
        return None

    if not webhook_url:
        log.warning(
            "DISCORD_WEBHOOK_URL er ikke sat — fund logges kun til konsol og database. "
            "Sæt DISCORD_WEBHOOK_URL, DISCORD_WEBHOOK_URL_FILE eller "
            "DISCORD_WEBHOOK_URL_FROM_ENV for at få beskeder i Discord."
        )
        return None

    return DiscordNotifier(webhook_url)


def _install_signal_handlers(state: dict) -> None:
    def handle(signum, _frame):
        log.info("Fik signal %s — afslutter pænt", signum)
        state["stop"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def run_forever(
    *,
    interval_seconds: int = MIN_SCRAPE_INTERVAL_SECONDS,
    config_path: str | None = None,
    store_path: str = "data/auction_hunter.db",
    dry_run: bool = False,
) -> None:
    """Kør i loop med det lovlige minimumsinverval."""
    if interval_seconds < MIN_SCRAPE_INTERVAL_SECONDS:
        raise ConfigError(
            f"Intervallet må ikke være under {MIN_SCRAPE_INTERVAL_SECONDS} sekunder (15 min) — "
            "auktionshusets vilkår tillader højst ét scrape hvert 15. minut."
        )

    state = {"stop": False}
    _install_signal_handlers(state)

    with Store(store_path) as store:
        while not state["stop"]:
            started = time.monotonic()
            try:
                # Konfigurationen genlæses hver gang, så rettelser i
                # interests.yml slår igennem uden genstart.
                config = load_config(config_path)
                notifier = resolve_notifier(dry_run=dry_run)
                stats = run_once(config, store, notifier)
                log.info(
                    "Kørsel færdig: %d auktioner, %d lots, %d fund (%d nye), %d sendt",
                    stats.auctions, stats.lots, stats.matches, stats.new_matches, stats.notified,
                )
            except (ConfigError, ScrapeError) as exc:
                log.error("Kørsel fejlede: %s", exc)
            except Exception:
                log.exception("Uventet fejl i kørsel")

            if state["stop"]:
                break

            elapsed = time.monotonic() - started
            sleep_for = max(0.0, interval_seconds - elapsed)
            log.info("Næste kørsel om %.0f sekunder", sleep_for)
            # Sov i små bidder, så SIGTERM håndteres hurtigt.
            deadline = time.monotonic() + sleep_for
            while not state["stop"] and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
