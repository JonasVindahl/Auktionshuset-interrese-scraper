"""Kørsel: scrape -> match -> husk -> notificér.

Intervallet er bevidst ikke konfigurerbart under 15 minutter, fordi
auktionshusets vilkår kræver det. ``MIN_SCRAPE_INTERVAL_SECONDS`` kan derfor
kun overskrides opad.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import images
from .classifier import Classifier, ClassifierSettings, OpenAICompatibleClient
from .config import (
    MIN_SCRAPE_INTERVAL_SECONDS,
    Config,
    ConfigError,
    Profile,
    Source,
    load_config,
)
from .matcher import Match, last_chance, match_all, sort_matches
from .notifier import DiscordNotifier, LastChanceAlert, PriceAlert, send_digest
from .scraper import Lot, ScrapeError, Scraper
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

# Hvor laenge en prisadvarsel hviler pr. lot. Uden den ville hvert budloft i en
# travl auktion give en besked, og saa slår man ordningen fra igen.
PRICE_ALERT_COOLDOWN_HOURS = 12

# Hvor taet paa hammerslag et fulgt lot skal vaere foer "sidste chance".
LAST_CHANCE_HOURS = 1.0


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
    details_fetched: int = 0
    lots_with_ends: int = 0
    ends_parse_failures: int = 0
    # Hvad koerslen faktisk kostede i HTTP-kald, og hvor mange sider
    # auktionslisten havde. Falder sidetallet til 1 uden at auktionerne bliver
    # faerre, er pagineringen sandsynligvis brudt igen.
    requests: int = 0
    auction_pages: int = 0
    price_alerts: int = 0
    last_chance: int = 0
    errors: list[str] = field(default_factory=list)


def _fetch_details(
    config: Config,
    store: Store,
    scraper: Scraper,
    matches: list[Match],
    stats: RunStats,
) -> dict[str, tuple[str, ...]]:
    """Hent lot-siderne for nye fund og gem hvad de sagde om standen.

    Returnerer flagene pr. lot, så notifikationen kan bære dem med det samme.
    """
    settings = config.details
    lots = [match.lot for match in matches]
    missing = set(store.lots_without_details([lot.lot_id for lot in lots]))
    flags: dict[str, tuple[str, ...]] = {}

    for lot in lots:
        if lot.lot_id not in missing:
            continue
        if stats.details_fetched >= settings.max_per_run:
            log.info("Loftet for lot-sider nået (%d)", settings.max_per_run)
            break
        details = scraper.fetch_details(lot)
        # Markeres som forsøgt uanset udfaldet: en underside der er væk skal
        # ikke hentes igen hver kørsel i månedsvis.
        store.record_details(lot.lot_id, details.text, details.flags)
        stats.details_fetched += 1
        if details.flags:
            flags[lot.lot_id] = details.flags
        if settings.pause_seconds:
            time.sleep(settings.pause_seconds)

    return flags


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


def check_blindness(
    stats: RunStats, store: Store, region_label: str = "Sjælland"
) -> str | None:
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
            f"Der blev ikke fundet en enkelt aktiv auktion i {region_label}. "
            "Tjek om auktionshuset har ændret deres side, eller om der "
            "midlertidigt ikke er aktive auktioner."
        )

    if (stats.ends_parse_failures >= 5
            and stats.ends_parse_failures >= stats.lots_with_ends * 0.5):
        return (
            "**Agenten kan ikke læse sluttidspunkter.**\n"
            f"{stats.ends_parse_failures} af {stats.lots_with_ends} lots havde et "
            "data-ends der ikke kunne parses. Formatet på auktionshusets side "
            "er sandsynligvis ændret. Tjek scraper.parse_ends."
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


def alert_if_blind(
    stats: RunStats,
    store: Store,
    notifier: DiscordNotifier | None,
    region_label: str = "Sjælland",
) -> bool:
    """Advar om blindhed, men højst én gang i døgnet."""
    message = check_blindness(stats, store, region_label)
    if message is None or notifier is None:
        return False

    last = store.get_meta(BLIND_ALERT_KEY)
    if last is not None:
        try:
            elapsed = datetime.now(UTC) - datetime.fromisoformat(last)
            if elapsed.total_seconds() < BLIND_ALERT_COOLDOWN_HOURS * 3600:
                log.warning("Blindheds-advarsel undertrykt (sendt for nylig): %s", last)
                return False
        except ValueError:
            pass

    log.error("BLINDHEDS-ADVARSEL: %s", message.replace("\n", " "))
    if notifier.send_text(message):
        store.set_meta(
            BLIND_ALERT_KEY, datetime.now(UTC).isoformat(timespec="seconds")
        )
        stats.blind_alert_sent = True
        return True
    return False


def price_alerts_enabled() -> bool:
    raw = os.environ.get("PRICE_ALERTS", "").strip().lower()
    return raw not in ("0", "false", "nej", "no", "off")


def select_price_alerts(
    matches: list[Match],
    actions: dict[str, str],
    states: dict[str, tuple[int | None, str | None]],
    *,
    now: datetime | None = None,
    cooldown_hours: float = PRICE_ALERT_COOLDOWN_HOURS,
) -> list[PriceAlert]:
    """Hvilke fulgte lots er steget nok til at give en besked?

    Foerste gang et lot ses, findes der ingen raekke, og der sendes intet: den
    pris bliver baseline. En stigning foer det tidspunkt kan vi ikke vide noget
    om. Derefter kraever en besked at prisen er steget, at lot'et stadig er
    aktivt, og at cooldown er udloebet.
    """
    now = now or datetime.now(UTC)
    out: list[PriceAlert] = []
    for match in matches:
        lot = match.lot
        if actions.get(lot.lot_id) not in ("watch", "bid"):
            continue
        if lot.ends_at is not None and lot.ends_at <= now:
            continue
        state = states.get(lot.lot_id)
        if state is None:
            continue
        old_cost, last_alerted = state
        cost = match.cost
        if old_cost is None or cost <= old_cost:
            continue
        if last_alerted:
            try:
                elapsed = (now - datetime.fromisoformat(last_alerted)).total_seconds()
            except ValueError:
                elapsed = None
            if elapsed is not None and elapsed < cooldown_hours * 3600:
                continue
        out.append(
            PriceAlert(
                lot_id=lot.lot_id,
                title=lot.title,
                url=lot.url,
                old_cost=old_cost,
                new_cost=cost,
                ends_at=lot.ends_at.isoformat(timespec="seconds") if lot.ends_at else None,
            )
        )
    return out


def maybe_price_alerts(
    store: Store,
    notifier: DiscordNotifier | None,
    matches: list[Match],
    stats: RunStats,
) -> None:
    """Send besked naar prisen stiger paa et lot brugeren selv foelger."""
    if notifier is None or not matches or not price_alerts_enabled():
        return

    actions = store.feedback_actions([m.lot.lot_id for m in matches])
    watched = [m for m in matches if actions.get(m.lot.lot_id) in ("watch", "bid")]
    if not watched:
        return

    states = store.price_alert_state([m.lot.lot_id for m in watched])
    alerts = select_price_alerts(matches, actions, states)
    sent = bool(alerts) and notifier.send_price_alerts(alerts)
    alerted = {a.lot_id for a in alerts}
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")

    for match in watched:
        lot_id = match.lot.lot_id
        if sent and lot_id in alerted:
            store.mark_price_alerted(lot_id, match.cost, now_iso)
        elif lot_id not in alerted:
            # Ingen besked endnu: hold baseline opdateret, saa naeste stigning
            # maales fra den nyeste pris.
            store.set_price_baseline(lot_id, match.cost)
        # Fejlede beskeden, roeres prisen ikke, saa stigningen forsoeges igen.

    if sent:
        stats.price_alerts = len(alerts)


def last_chance_alerts_enabled() -> bool:
    raw = os.environ.get("LAST_CHANCE_ALERTS", "").strip().lower()
    return raw not in ("0", "false", "nej", "no", "off")


def last_chance_window_hours() -> float:
    raw = os.environ.get("LAST_CHANCE_HOURS", "").strip()
    try:
        return float(raw) if raw else LAST_CHANCE_HOURS
    except ValueError:
        return LAST_CHANCE_HOURS


def select_last_chance(
    matches: list[Match],
    actions: dict[str, str],
    sent: set[str],
    *,
    now: datetime | None = None,
    within_hours: float | None = None,
) -> list[LastChanceAlert]:
    """Fulgte lots hvor hammerslaget er taet paa, og vi ikke har sagt det.

    Beskeden skal kun komme én gang pr. lot. Ellers ville den komme hvert 15.
    minut i den sidste time.
    """
    now = now or datetime.now(UTC)
    hours = LAST_CHANCE_HOURS if within_hours is None else within_hours
    out: list[LastChanceAlert] = []
    for match in matches:
        lot = match.lot
        if actions.get(lot.lot_id) not in ("watch", "bid"):
            continue
        if lot.lot_id in sent:
            continue
        if not last_chance(lot, hours, now=now):
            continue
        out.append(
            LastChanceAlert(
                lot_id=lot.lot_id,
                title=lot.title,
                url=lot.url,
                ends_at=lot.ends_at.isoformat(timespec="seconds") if lot.ends_at else None,
            )
        )
    return out


def maybe_last_chance(
    store: Store,
    notifier: DiscordNotifier | None,
    matches: list[Match],
    stats: RunStats,
) -> None:
    """Advar én gang naar et fulgt lot er taet paa hammerslag."""
    if notifier is None or not matches or not last_chance_alerts_enabled():
        return

    actions = store.feedback_actions([m.lot.lot_id for m in matches])
    watched = [m for m in matches if actions.get(m.lot.lot_id) in ("watch", "bid")]
    if not watched:
        return

    sent = store.last_chance_sent([m.lot.lot_id for m in watched])
    alerts = select_last_chance(
        matches, actions, sent, within_hours=last_chance_window_hours()
    )
    if not alerts:
        return

    if notifier.send_last_chance(alerts):
        store.mark_last_chance(
            [alert.lot_id for alert in alerts],
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        stats.last_chance = len(alerts)


def maybe_prune(store: Store) -> dict[str, int]:
    """Ryd historik, men højst én gang i døgnet.

    Oprydning er ikke gratis på en stor database, så den køres sjældent i
    stedet for hver 15. minut. Tidsstemplet ligger i meta, så det overlever
    genstart af containeren.
    """
    last = store.get_meta(PRUNE_KEY)
    if last is not None:
        try:
            elapsed = datetime.now(UTC) - datetime.fromisoformat(last)
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

    store.set_meta(PRUNE_KEY, datetime.now(UTC).isoformat(timespec="seconds"))
    return removed


def _match_profiles(lots: list[Lot], config: Config) -> list[Match]:
    """Match hvert lot mod hver aktiv profil.

    Uden 'profiles' i konfigurationen er der én implicit profil, så resultatet
    er identisk med at matche mod topniveauet direkte.
    """
    results: list[Match] = []
    for profile in config.active_profiles():
        results.extend(
            match_all(lots, config, opening_bid=config.opening_bid, profile=profile)
        )
    return results


def _dedupe_by_lot(matches: list[Match]) -> list[Match]:
    """Ét fund pr. lot, til de varsler der ikke hoerer til en enkelt profil.

    Med flere profiler kan samme lot optraede flere gange i listen. Pris- og
    sidste-chance-varsler gaar paa lot'et, ikke paa profilen, så de må ikke
    sende det samme to gange.
    """
    seen: set[str] = set()
    unique: list[Match] = []
    for match in matches:
        if match.lot.lot_id in seen:
            continue
        seen.add(match.lot.lot_id)
        unique.append(match)
    return unique


def _notifier_for_profile(
    profile: Profile | None, default: DiscordNotifier
) -> DiscordNotifier | None:
    """Profilens egen webhook hvis den peger på en, ellers den faelles."""
    if profile is None or not profile.webhook_env:
        return default
    try:
        url = get_secret(profile.webhook_env)
    except SecretError as exc:
        log.error(
            "Profilen %s peger på %s, men den kunne ikke læses: %s",
            profile.key, profile.webhook_env, exc,
        )
        return None
    if not url:
        log.warning(
            "Profilen %s peger på %s, som ikke er sat — springer den over",
            profile.key, profile.webhook_env,
        )
        return None
    return DiscordNotifier(url.strip())


def _notify_profiles(
    config: Config,
    default: DiscordNotifier,
    matches: list[Match],
    details_flags: dict[str, tuple[str, ...]],
    region_label: str,
    store: Store,
) -> int:
    """Send nye fund, grupperet pr. profil.

    En profil kan pege på sin egen webhook, så HiFi og vaerktoj lander i hver
    sin kanal. Kun det der faktisk blev leveret markeres, så en fejlet besked
    forsoeges igen naeste gang i stedet for at gå tabt.
    """
    groups: dict[str, list[Match]] = {}
    for match in matches:
        groups.setdefault(match.profile_key, []).append(match)

    sent_total = 0
    for key, group in groups.items():
        profile = config.profile(key)
        client = _notifier_for_profile(profile, default)
        if client is None:
            continue

        label = profile.label if profile else ""
        where = f"{label} · {region_label}" if label else region_label
        heading = (
            f"**{len(group)} nyt fund** i {where}"
            if len(group) == 1
            else f"**{len(group)} nye fund** i {where}"
        )
        result = client.send_matches(group, heading=heading, details=details_flags)
        for match in result.sent:
            store.mark_notified(
                match.lot.lot_id, match.category.key, match.cost, match.profile_key
            )
        sent_total += len(result.sent)
    return sent_total


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
        # Scraperen taeller hvor mange data-ends der ikke kunne parses. Uden
        # det fjerner et aendret format alle deadlines uden en lyd.
        stats.lots_with_ends = int(getattr(scraper, "lots_with_ends", 0))
        stats.ends_parse_failures = int(getattr(scraper, "ends_parse_failures", 0))
        stats.requests = int(getattr(scraper, "requests", 0))
        stats.auction_pages = int(getattr(scraper, "auction_pages", 0))

        matches = sort_matches(_match_profiles(all_lots, config))
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

        # Lot-siderne hentes kun for fund, og kun hvis nogen har slået det til:
        # det er et ekstra kald til auktionshuset pr. lot. Loftet pr. kørsel og
        # pausen holder belastningen nede, og et svar der ikke kan hentes
        # markeres som forsøgt så det ikke prøves i det uendelige.
        details_flags: dict[str, tuple[str, ...]] = {}
        if config.details.enabled and new_matches:
            details_flags = _fetch_details(config, store, scraper, new_matches, stats)

        if new_matches and notifier is not None:
            stats.notified = _notify_profiles(
                config,
                notifier,
                new_matches,
                details_flags,
                config.source.region_label,
                store,
            )

        # Pris- og sidste-chance-varsler gaar paa lot'et, ikke på profilen, så
        # et lot der matcher to profiler må ikke give to ens beskeder.
        unique_matches = _dedupe_by_lot(matches)

        # Prisen kan stige på et lot brugeren selv følger. Det er den anden
        # slags ny information ud over et nyt fund.
        maybe_price_alerts(store, notifier, unique_matches, stats)

        # Og naar et fulgt lot er taet paa hammerslag, saa det ikke bliver
        # opdaget for sent.
        maybe_last_chance(store, notifier, unique_matches, stats)

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
        alert_if_blind(stats, store, notifier, config.source.region_label)

        store.finish_run(
            run_id,
            auctions=stats.auctions,
            lots=stats.lots,
            matches=stats.matches,
            new_matches=stats.new_matches,
            requests=stats.requests,
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
                    "Kørsel færdig: %d auktioner (%d sider), %d lots, %d fund "
                    "(%d nye), %d sendt, %d HTTP-kald",
                    stats.auctions, stats.auction_pages, stats.lots, stats.matches,
                    stats.new_matches, stats.notified, stats.requests,
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
