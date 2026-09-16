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
from typing import Callable

from . import images
from .config import MIN_SCRAPE_INTERVAL_SECONDS, Config, ConfigError, Source, load_config
from .classifier import Classifier, ClassifierSettings, OpenAICompatibleClient
from .matcher import Match, match_all, sort_matches
from .notifier import DiscordNotifier, send_digest
from .scraper import ScrapeError, Scraper
from .secrets import SecretError, get_secret, redact
from .storage import Store

log = logging.getLogger(__name__)

# Hvor mange 'måske'-fund der samles op til ét digest pr. kørsel.
MAX_DIGEST_ITEMS = 25

# Hvor stort et fald i antallet af lots der udløser en blindheds-advarsel.
# Et fald til under denne andel af det normale antal betyder næsten altid at
# sidens opbygning er ændret, ikke at udbuddet er forsvundet.
BLIND_THRESHOLD = 0.1

# Hvor ofte historikken ryddes. Auktioner løber i uger, og oprydning er ikke
# gratis på en stor database, så det gøres dagligt frem for hver 15. minut.
PRUNE_INTERVAL_HOURS = 24

# Nøglen i meta-tabellen der husker hvornår der sidst blev advaret, så vi ikke
# sender en advarsel hver 15. minut i ugevis.
BLIND_ALERT_KEY = "blind_alert_at"

# Nøglen der husker hvornår historikken sidst blev ryddet.
PRUNE_KEY = "pruned_at"

# Minimum mellem to advarsler om samme problem.
BLIND_ALERT_COOLDOWN_HOURS = 24


@dataclass
class RunStats:
    auctions: int = 0
    lots: int = 0
    matches: int = 0
    new_matches: int = 0
    notified: int = 0
    rejected_by_ai: int = 0
    review_queued: int = 0
    digested: int = 0
    deferred: int = 0
    blind_alert_sent: bool = False
    pruned: int = 0
    images_cached: int = 0
    errors: list[str] = field(default_factory=list)


def build_classifier(config: Config, store: Store) -> Classifier | None:
    """Sæt AI-trinnet op hvis det er slået til og nøglen findes.

    Nøglen opløses ved hver kørsel, så en roteret hemmelighed virker uden
    genstart. Er trinnet slået til men mangler nøglen, logges det tydeligt, og
    agenten kører videre uden AI — bedre end at falde helt om.
    """
    settings = config.classifier
    if not settings.configured:
        return None

    try:
        api_key = get_secret("CLASSIFIER_API_KEY") or get_secret("OPENAI_API_KEY")
    except SecretError as exc:
        log.error("AI-trinnet er slået til, men nøglen kunne ikke læses: %s", exc)
        return None

    if not api_key:
        log.warning(
            "AI-trinnet er slået til i interests.yml, men CLASSIFIER_API_KEY (eller "
            "OPENAI_API_KEY) er ikke sat. Kører uden AI. Sæt CLASSIFIER_API_KEY, "
            "CLASSIFIER_API_KEY_FILE eller CLASSIFIER_API_KEY_FROM_ENV."
        )
        return None

    log.info("AI-trinnet er aktivt (%s via %s)", settings.model, redact(api_key))
    runtime = ClassifierSettings(
        enabled=True,
        model=settings.model,
        base_url=settings.base_url,
        api_key=api_key,
        profile=settings.profile,
        max_per_run=settings.max_per_run,
        timeout=settings.timeout,
    )
    client = OpenAICompatibleClient(
        base_url=settings.base_url,
        api_key=api_key,
        model=settings.model,
        timeout=settings.timeout,
    )
    return Classifier(client, runtime, store)


def check_blindness(stats: RunStats, store: Store) -> str | None:
    """Opdager at agenten er holdt op med at se lots.

    Dette er den farligste langsigtede fejl: auktionshuset ændrer deres HTML, og
    ``fetch_lots`` returnerer tomt. Uden dette tjek melder kørslen succes med 0
    fund, og brugeren opdager først uger senere at der aldrig kom noget.
    """
    if stats.errors:
        return None  # Fejlen er allerede rapporteret i sig selv.

    if stats.auctions == 0:
        return (
            "**Agenten er holdt op med at finde auktioner.**\n"
            "Der blev ikke fundet en enkelt aktiv auktion i Sjælland. "
            "Tjek om auktionshuset har ændret deres side, eller om der "
            "midlertidigt ikke er aktive auktioner."
        )

    baseline = store.last_successful_lot_count()
    if baseline and stats.lots < baseline * BLIND_THRESHOLD:
        return (
            f"**Agenten er muligvis blevet blind.**\n"
            f"Fandt {stats.lots} lots mod normalt omkring {baseline}. "
            f"Sandsynligvis har auktionshuset ændret deres HTML, så udtrækket "
            f"ikke matcher længere. Tjek `scraper.py`."
        )
    return None


def alert_if_blind(stats: RunStats, store: Store, notifier: DiscordNotifier | None) -> bool:
    """Advar om blindhed, men højst én gang i døgnet."""
    message = check_blindness(stats, store)
    if message is None or notifier is None:
        return False

    last = store.get_meta(BLIND_ALERT_KEY)
    if last is not None:
        try:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            if elapsed.total_seconds() < BLIND_ALERT_COOLDOWN_HOURS * 3600:
                log.warning("Blindheds-advarsel undertrykt (sendt for nylig): %s", last)
                return False
        except ValueError:
            pass

    log.error("BLINDHEDS-ADVARSEL: %s", message.replace("\n", " "))
    if notifier.send_text(message):
        store.set_meta(
            BLIND_ALERT_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        stats.blind_alert_sent = True
        return True
    return False


def maybe_prune(store: Store) -> dict[str, int]:
    """Ryd historik, men højst én gang i døgnet.

    Oprydning er ikke gratis på en stor database, så den køres sjældent i
    stedet for hver 15. minut. Tidsstemplet ligger i meta, så det overlever
    genstart af containeren.
    """
    last = store.get_meta(PRUNE_KEY)
    if last is not None:
        try:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            if elapsed.total_seconds() < PRUNE_INTERVAL_HOURS * 3600:
                return {}
        except ValueError:
            pass

    removed = store.prune()

    # Billeder for lots der har været afsluttet længe. De ryddes i samme takt
    # som historikken, så der kun er ét tidspunkt hvor disken bliver rørt.
    try:
        gone = images.prune(store.conn, store.path)
        if gone:
            removed["images"] = gone
    except Exception:
        log.exception("Billedoprydning fejlede — fortsætter")

    store.set_meta(PRUNE_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return removed


def run_once(
    config: Config,
    store: Store,
    notifier: DiscordNotifier | None,
    *,
    scraper_factory: Callable[[Source], Scraper] = Scraper,
) -> RunStats:
    """Én fuld gennemkørsel.

    ``scraper_factory`` gør det muligt at køre hele kæden i test uden at hente
    sider. I drift bruges ``Scraper`` som den er.
    """
    stats = RunStats()
    run_id = store.start_run()
    scraper = scraper_factory(config.source)

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

        matches = sort_matches(match_all(all_lots, config, opening_bid=config.opening_bid))
        stats.matches = len(matches)

        # Totalen inkl. salær og moms gemmes sammen med lot'et. Uden den står
        # last_total tomt, og så regner pris-filtre og statistik på hammerprisen
        # mens kortet viser den reelle pris. To tal for den samme vare.
        store.record_lots(all_lots, {m.lot.lot_id: m.cost for m in matches})

        new_matches: list[Match] = store.filter_new(matches)
        stats.new_matches = len(new_matches)

        # AI-trinnet kører kun på nye fund, så omkostningen følger antallet af
        # fund og ikke antallet af lots.
        classifier = build_classifier(config, store)
        if classifier is not None and new_matches:
            try:
                verdicts = classifier.classify_matches(new_matches)
            except Exception:
                # AI-trinnet er en ekstra silning. Fejler det, sendes fundene
                # videre ufiltreret — støj er billigere end et tabt fund.
                log.exception("AI-trinnet fejlede — fortsætter uden filtrering")
            else:
                stats.rejected_by_ai = len(verdicts.rejected)
                stats.review_queued = len(verdicts.review)
                stats.deferred = len(verdicts.deferred)
                for match in verdicts.rejected:
                    log.info("AI afviste: %s", match.lot.title[:70])
                new_matches = verdicts.accepted

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

        # Grænsetilfælde sendes samlet i ét digest i stedet for én ad gangen,
        # så de ikke støjer i nuet. Først når beskeden er leveret markeres de,
        # så et fejlet forsøg prøves igen.
        if notifier is not None:
            pending = store.pending_reviews(limit=MAX_DIGEST_ITEMS)
            if pending and send_digest(notifier, pending):
                store.mark_digested([row["input_hash"] for row in pending])
                stats.digested = len(pending)

        # Blindheds-tjekket ligger efter notifikationerne, så en advarsel ikke
        # fortrænger et rigtigt fund. Det fanger at udtrækket er holdt op med at
        # virke, hvilket ellers ligner "der er ingen fund i dag" i det uendelige.
        alert_if_blind(stats, store, notifier)

        store.finish_run(
            run_id,
            auctions=stats.auctions,
            lots=stats.lots,
            matches=stats.matches,
            new_matches=stats.new_matches,
            error="; ".join(stats.errors) or None,
        )

        # Billederne hentes efter notifikationerne, så en langsom eller død
        # billedserver aldrig kan forsinke en besked. Kun fund og
        # gennemsynskandidater hentes — auktionshuset fjerner billedet når
        # lot'et lukker, og så er Udløbet-fanen uden billeder for altid.
        try:
            stats.images_cached = images.cache_pending(
                store.conn, store.path, session=scraper.session
            )
        except Exception:
            log.exception("Billedcachen fejlede — fortsætter")

        # Oprydning kører efter kørslen, så en fejl her ikke koster fund.
        try:
            removed = maybe_prune(store)
            stats.pruned = sum(removed.values())
            if removed:
                log.info("Ryddede gammel historik: %s", removed)
        except Exception:
            log.exception("Oprydning fejlede — fortsætter")
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
