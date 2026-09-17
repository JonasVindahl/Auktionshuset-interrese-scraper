"""Vedvarende hukommelse i SQLite.

Databasen er en enkelt fil, så den er nem at bakke op og flytte. Den holder
styr på tre ting:

* Hvilke lots der er set, og til hvilken pris (så vi kan se bud give op)
* Hvilke fund der allerede er sendt til Discord (så vi ikke spammer)
* Hver kørsel, så fejl kan spores

Dertil kommer AI-trinets hukommelse: klassificeringer caches på et hash af
titel, kategori, model og version, og 'måske'-svar lægges i en kø til digest.
Begge dele gør at sprogmodellen ikke skal spørges om det samme to gange.

WAL-tilstand bruges, så læsning ikke blokeres af skrivning.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .fees import estimate as price_estimate
from .matcher import Match
from .scraper import Lot
from .textmatch import normalize, normalize_loose

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    lot_id        TEXT PRIMARY KEY,
    auction_id    TEXT NOT NULL,
    auction_title TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    lot_number    TEXT NOT NULL DEFAULT '',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    first_bid     INTEGER,
    last_bid      INTEGER,
    last_total    INTEGER,
    ends_at       TEXT,
    image_url     TEXT NOT NULL DEFAULT '',
    -- Kolonnerne herunder kom til senere og staar ogsaa i MIGRATIONS, som er
    -- vejen ind i en database der allerede findes. De skal staa begge steder:
    -- her, saa en ny database ikke oprettes for straks at blive ALTER'et, og
    -- der, saa en gammel database faar dem.
    details       TEXT NOT NULL DEFAULT '',
    details_flags TEXT NOT NULL DEFAULT '',
    details_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_history (
    lot_id      TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    bid         INTEGER,
    total       INTEGER,
    PRIMARY KEY (lot_id, observed_at)
);

CREATE TABLE IF NOT EXISTS notifications (
    lot_id       TEXT NOT NULL,
    category_key TEXT NOT NULL,
    profile_key  TEXT NOT NULL DEFAULT 'standard',
    sent_at      TEXT NOT NULL,
    cost         INTEGER,
    PRIMARY KEY (lot_id, category_key, profile_key)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    auctions      INTEGER NOT NULL DEFAULT 0,
    lots          INTEGER NOT NULL DEFAULT 0,
    matches       INTEGER NOT NULL DEFAULT 0,
    new_matches   INTEGER NOT NULL DEFAULT 0,
    -- Hvor mange HTTP-kald koerslen kostede. Projektet beskrev sig selv som
    -- "ét scrape hvert 15. minut"; i praksis er det listens sider plus mindst
    -- ét katalogkald pr. auktion. Tallet maales, saa paastanden kan efterproeves.
    requests      INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_lots_ends ON lots(ends_at);
CREATE INDEX IF NOT EXISTS idx_notifications_sent ON notifications(sent_at);

-- AI-trinet: cachede klassificeringer. Noeglen er et hash af titel, kategori,
-- model og classifier-version, saa en aendret prompt ugyldiggoer cachen.
CREATE TABLE IF NOT EXISTS classifications (
    input_hash   TEXT PRIMARY KEY,
    lot_id       TEXT NOT NULL,
    category_key TEXT NOT NULL,
    version      TEXT NOT NULL,
    model        TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

-- 'maaske'-fund der endnu ikke er sendt i et digest.
CREATE TABLE IF NOT EXISTS review_queue (
    input_hash   TEXT PRIMARY KEY,
    lot_id       TEXT NOT NULL,
    category_key TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    digested_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_pending ON review_queue(digested_at);
CREATE INDEX IF NOT EXISTS idx_classifications_lot ON classifications(lot_id);
CREATE INDEX IF NOT EXISTS idx_lots_last_seen ON lots(last_seen);

-- Små nøgle/værdi-par til tilstand der ikke hører til nogen af tabellerne,
-- fx hvornår der sidst blev advaret om at agenten er blevet blind.
-- En ny tabel er sikker at tilføje: CREATE TABLE IF NOT EXISTS rører ikke
-- eksisterende databaser, i modsætning til en ny kolonne.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Manuel feedback på sendte fund: skip/bid/bought.
-- Bruges til AI-træning og til at markere hvad der er gjort.
-- En ny tabel er sikker at tilføje uden at røre eksisterende data.
CREATE TABLE IF NOT EXISTS feedback (
    lot_id       TEXT NOT NULL,
    category_key TEXT NOT NULL,
    action       TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (lot_id, category_key)
);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);

-- Fritekst-indeks over alle sete lots, ikke kun dem der matchede.
-- 'normalized' indeholder titlen kørt gennem textmatch, så en søgning på
-- 'hojttaler' også finder 'højttaler'. Tabellen fyldes fra record_lot og er
-- derfor altid i sync med 'lots'.
CREATE VIRTUAL TABLE IF NOT EXISTS lots_fts USING fts5(
    lot_id UNINDEXED,
    title,
    normalized,
    auction_title,
    tokenize='unicode61 remove_diacritics 2'
);

-- Et lot brugeren foelger kan stige i pris, og det er ny information. Tabellen
-- husker hvilken pris der sidst blev sendt besked om, og hvornaar. alerted_at
-- er tom naar raekken kun er en baseline: den foerste pris vi saa, som en
-- senere stigning maales fra.
CREATE TABLE IF NOT EXISTS price_alerts (
    lot_id     TEXT PRIMARY KEY,
    cost       INTEGER,
    alerted_at TEXT NOT NULL DEFAULT ''
);

-- Lots brugeren foelger hvor der er sendt en "sidste chance"-besked. Uden
-- denne husker vi ikke at vi allerede har sagt det, og beskeden ville komme
-- hvert 15. minut i den sidste time.
CREATE TABLE IF NOT EXISTS last_chance_alerts (
    lot_id  TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
"""

# Kolonner der er kommet til efter de første databaser blev oprettet.
# ``CREATE TABLE IF NOT EXISTS`` rører ikke en eksisterende tabel, så en ny
# kolonne skal tilføjes eksplicit — ellers fejler agenten først i drift, på en
# database der har kørt i månedsvis.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("lots", "image_url", "ALTER TABLE lots ADD COLUMN image_url TEXT NOT NULL DEFAULT ''"),
    # Lot-sidens tekst og de stand-signaler der blev fundet i den. Hentes kun
    # for fund, og kun hvis det er slået til: det er et ekstra kald til
    # auktionshuset pr. lot.
    ("lots", "details", "ALTER TABLE lots ADD COLUMN details TEXT NOT NULL DEFAULT ''"),
    ("lots", "details_flags", "ALTER TABLE lots ADD COLUMN details_flags TEXT NOT NULL DEFAULT ''"),
    ("lots", "details_at", "ALTER TABLE lots ADD COLUMN details_at TEXT NOT NULL DEFAULT ''"),
    ("runs", "requests", "ALTER TABLE runs ADD COLUMN requests INTEGER NOT NULL DEFAULT 0"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Tilføj manglende kolonner til en eksisterende database.

    Idempotent: kolonnen tilføjes kun hvis den ikke allerede findes, så det er
    gratis at køre ved hver opstart.
    """
    for table, column, statement in MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            log.info("Migrerer: tilføjer %s.%s", table, column)
            conn.execute(statement)

    _migrate_notification_profiles(conn)
    _backfill_search_index(conn)


def _migrate_notification_profiles(conn: sqlite3.Connection) -> None:
    """Giv notifikationer en profil-noegle og en tre-delt primaernoegle.

    SQLite kan ikke aendre en primaernoegle, så tabellen bygges om. Raekker fra
    foer profilerne hoerer til standardprofilen, hvilket er praecis den adfaerd
    de havde: uden 'profiles' i konfigurationen er der kun den ene.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")}
    if not existing or "profile_key" in existing:
        return

    log.info("Migrerer: bygger notifications om med profile_key")
    conn.execute("ALTER TABLE notifications RENAME TO notifications_old")
    conn.execute(
        """CREATE TABLE notifications (
               lot_id       TEXT NOT NULL,
               category_key TEXT NOT NULL,
               profile_key  TEXT NOT NULL DEFAULT 'standard',
               sent_at      TEXT NOT NULL,
               cost         INTEGER,
               PRIMARY KEY (lot_id, category_key, profile_key)
           )"""
    )
    conn.execute(
        """INSERT INTO notifications (lot_id, category_key, profile_key, sent_at, cost)
           SELECT lot_id, category_key, 'standard', sent_at, cost FROM notifications_old"""
    )
    conn.execute("DROP TABLE notifications_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_sent ON notifications(sent_at)")


def _search_text(title: str) -> str:
    """Den søgbare form af en titel.

    Begge danske foldninger indekseres: ``normalize`` giver 'hoejttaler' og
    ``normalize_loose`` giver 'hojttaler'. FTS5 deler på mellemrum, så begge
    former bliver til søgbare ord, og brugeren kan skrive hvad der falder dem
    naturligt.
    """
    strict = normalize(title)
    loose = normalize_loose(title)
    return strict if strict == loose else f"{strict} {loose}"


def _backfill_search_index(conn: sqlite3.Connection) -> None:
    """Fyld søgeindekset for lots der blev gemt før indekset fandtes.

    Kører kun når der faktisk mangler noget, så en database der er i sync
    betaler prisen for ét COUNT og ikke mere.
    """
    lots = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    indexed = conn.execute("SELECT COUNT(*) FROM lots_fts").fetchone()[0]
    if lots == indexed:
        return

    log.info("Bygger søgeindeks: %d lots mangler", lots - indexed)
    conn.execute("DELETE FROM lots_fts")
    conn.execute(
        """INSERT INTO lots_fts (lot_id, title, normalized, auction_title)
           SELECT lot_id, title, '', auction_title FROM lots"""
    )
    # Den normaliserede form beregnes i Python; SQLite kan ikke folde dansk.
    rows = conn.execute("SELECT lot_id, title FROM lots").fetchall()
    conn.executemany(
        "UPDATE lots_fts SET normalized=? WHERE lot_id=?",
        [(_search_text(row["title"] or ""), row["lot_id"]) for row in rows],
    )

# Hvor længe historik holdes. Auktioner løber i uger, så et halvt år er rigeligt
# til at se et mønster, og grænsen holder databasen fra at vokse i det uendelige
# — uden den ville pris-historikken alene blive flere GB om året.
DEFAULT_RETENTION_DAYS = 180

# Filen der siger at en agent bruger denne database. Se Store.touch_heartbeat.
HEARTBEAT_SUFFIX = ".live"


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def utcnow_precise() -> str:
    """Tidsstempel med mikrosekunder, til pris-historikken.

    Primærnøglen i ``price_history`` er ``(lot_id, observed_at)``. Med kun
    sekund-præcision ville et lot hvis pris ændrede sig to gange inden for samme
    sekund — hvilket sker når en tests kører hurtigt, og når en auktion lukker
    med bud i sidste øjeblik — kun gemme den sidste observation.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class LotStats:
    """Hvad der er kendt om et lot fra tidligere kørsler."""

    lot_id: str
    first_seen: str
    first_bid: int | None
    last_bid: int | None
    notified: frozenset[str]


class Store:
    """SQLite-baseret hukommelse."""

    def __init__(self, path: str | Path = "data/auction_hunter.db") -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Webben skriver feedback gennem sin egen forbindelse, og den daglige
        # checkpoint kraever et oejeblik uden laas. Vent hellere end at fejle.
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(SCHEMA)
        _migrate(self.conn)
        self.conn.commit()
        # Livstegnet skrives foerst naar der startes en koersel, ikke bare
        # fordi filen aabnes: 'auction_hunter stats' og 'export' aabner ogsaa
        # en Store, og de siger intet om at en agent er i gang.
        self._heartbeat_mark: str | None = None

    # -- livstegn ----------------------------------------------------------

    def heartbeat_path(self) -> Path:
        return Path(str(self.path) + HEARTBEAT_SUFFIX)

    def touch_heartbeat(self) -> None:
        """Skriv at en agent er i live paa denne database.

        Gendannelse bytter databasefilen ud under en aaben forbindelse. Goer
        man det mens agenten koerer, beholder den den gamle inode og skriver
        videre i en slettet fil, saa gendannelsen forsvinder uden en lyd.
        --yes var indtil nu den eneste kontrol, og den er en afkrydsning.

        Et PID-tjek duer ikke: agenten og gendannelsen koerer typisk i hver sin
        container med hver sit PID-rum. Filens alder er det signal der faktisk
        krydser den graense, og den opdateres ved hver koersel.

        Kaldes fra start_run, ikke fra __init__: at aabne databasen for at se
        statistik betyder ikke at en agent skriver i den.
        """
        if str(self.path) == ":memory:":
            return
        mark = f"{os.getpid()} {utcnow_precise()}\n"
        try:
            self.heartbeat_path().write_text(mark, encoding="utf-8")
        except OSError:
            # Et manglende livstegn maa aldrig vaelte en koersel.
            log.debug("Kunne ikke skrive livstegn ved siden af %s", self.path)
            return
        self._heartbeat_mark = mark

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()
        self._release_heartbeat()

    def _release_heartbeat(self) -> None:
        """Fjern livstegnet, men kun hvis det stadig er vores eget.

        Der slettes kun praecis det vi selv skrev. En Store der aldrig har
        startet en koersel har intet maerke og roerer derfor ingenting, saa et
        kortlivet 'stats'-opslag ikke kan fjerne den koerende agents livstegn
        og faa restore til at tro at databasen er ledig.
        """
        if str(self.path) == ":memory:" or self._heartbeat_mark is None:
            return
        beat = self.heartbeat_path()
        try:
            if beat.read_text(encoding="utf-8") != self._heartbeat_mark:
                return
        except OSError:
            return
        with contextlib.suppress(OSError):
            beat.unlink(missing_ok=True)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- kørsler -----------------------------------------------------------

    def start_run(self) -> int:
        self.touch_heartbeat()
        with self._tx() as conn:
            cursor = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (utcnow(),))
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        auctions: int = 0,
        lots: int = 0,
        matches: int = 0,
        new_matches: int = 0,
        requests: int = 0,
        error: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """UPDATE runs SET finished_at=?, auctions=?, lots=?, matches=?,
                   new_matches=?, requests=?, error=? WHERE run_id=?""",
                (utcnow(), auctions, lots, matches, new_matches, requests,
                 error, run_id),
            )

    # -- lots --------------------------------------------------------------

    def record_lot(self, lot: Lot, total: int | None) -> None:
        """Gem lot og dets aktuelle pris; opdater pris-historik ved ændring."""
        with self._tx() as conn:
            self._write_lot(conn, lot, total)

    def record_lots(self, lots: list[Lot], totals: dict[str, int | None] | None = None) -> None:
        """Gem alle lots i én transaktion.

        En kørsel giver typisk omkring 2.000 lots. Én transaktion pr. lot ville
        betyde 2.000 commits og dermed 2.000 fsync pr. kørsel; her er der ét.
        """
        if not lots:
            return
        totals = totals or {}
        with self._tx() as conn:
            for lot in lots:
                self._write_lot(conn, lot, totals.get(lot.lot_id))

    def _write_lot(self, conn: sqlite3.Connection, lot: Lot, total: int | None) -> None:
        """Skrivningen bag baade record_lot og record_lots. Deler transaktion."""
        now = utcnow()
        ends = lot.ends_at.isoformat(timespec="seconds") if lot.ends_at else None

        if total is None:
            # Kortet viser den reelle pris inkl. salær og moms. Gem samme tal,
            # saa et prisfilter rammer det brugeren ser i stedet for det raa bud.
            price = price_estimate(lot.current_bid, auction_title=lot.auction_title)
            total = price.current_total or price.entry_cost

        row = conn.execute(
            "SELECT last_bid, last_total FROM lots WHERE lot_id=?", (lot.lot_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO lots (lot_id, auction_id, auction_title, title, url,
                   lot_number, first_seen, last_seen, first_bid, last_bid, last_total,
                   ends_at, image_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lot.lot_id, lot.auction_id, lot.auction_title, lot.title, lot.url,
                    lot.lot_number, now, now, lot.current_bid, lot.current_bid, total, ends,
                    lot.image_url,
                ),
            )
            price_changed = True
        else:
            # Billedet kan mangle i ét udtræk (lazy loading) uden at være
            # forsvundet. Behold derfor det gamle, hvis det nye er tomt.
            conn.execute(
                """UPDATE lots SET last_seen=?, last_bid=?, last_total=?, ends_at=?,
                   title=?, url=?, image_url=COALESCE(NULLIF(?, ''), image_url)
                   WHERE lot_id=?""",
                (now, lot.current_bid, total, ends, lot.title, lot.url,
                 lot.image_url, lot.lot_id),
            )
            # Kun en faktisk prisændring er ny information. Uden dette skrev
            # hver kørsel en række pr. lot pr. 15. minut, og historikken
            # voksede til flere GB om året uden at sige noget nyt.
            price_changed = (row["last_bid"], row["last_total"]) != (lot.current_bid, total)
            if price_changed:
                log.info("Prisændring på %s: %s -> %s",
                         lot.lot_id, row["last_bid"], lot.current_bid)

        if price_changed:
            conn.execute(
                """INSERT OR REPLACE INTO price_history (lot_id, observed_at, bid, total)
                   VALUES (?,?,?,?)""",
                (lot.lot_id, utcnow_precise(), lot.current_bid, total),
            )

        # Søgeindekset. Kun titlen kan ændre sig, så den billige vej er at
        # slette og indsætte igen — FTS5 har ingen UPDATE der virker på en
        # ekstern nøgle.
        indexed = conn.execute(
            "SELECT title FROM lots_fts WHERE lot_id=?", (lot.lot_id,)
        ).fetchone()
        if indexed is None or indexed["title"] != lot.title:
            if indexed is not None:
                conn.execute("DELETE FROM lots_fts WHERE lot_id=?", (lot.lot_id,))
            conn.execute(
                """INSERT INTO lots_fts (lot_id, title, normalized, auction_title)
                   VALUES (?,?,?,?)""",
                (lot.lot_id, lot.title, _search_text(lot.title), lot.auction_title),
            )

    # -- notifikationer ----------------------------------------------------

    def already_notified(
        self, lot_id: str, category_key: str, profile_key: str = "standard"
    ) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM notifications
               WHERE lot_id=? AND category_key=? AND profile_key=?""",
            (lot_id, category_key, profile_key),
        ).fetchone()
        return row is not None

    def mark_notified(
        self,
        lot_id: str,
        category_key: str,
        cost: int | None,
        profile_key: str = "standard",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO notifications
                       (lot_id, category_key, profile_key, sent_at, cost)
                   VALUES (?,?,?,?,?)""",
                (lot_id, category_key, profile_key, utcnow(), cost),
            )

    def filter_new(self, matches: list[Match]) -> list[Match]:
        """Behold kun fund der ikke er sendt før.

        Noeglen er (lot, kategori, profil), så det samme lot godt kan give en
        besked pr. profil — fx HiFi til én kanal og vaerktoj til en anden — men
        aldrig to gange til den samme.
        """
        return [
            m for m in matches
            if not self.already_notified(m.lot.lot_id, m.category.key, m.profile_key)
        ]

    def lot_stats(self, lot_id: str) -> LotStats | None:
        row = self.conn.execute(
            "SELECT * FROM lots WHERE lot_id=?", (lot_id,)
        ).fetchone()
        if row is None:
            return None
        notified = frozenset(
            r["category_key"]
            for r in self.conn.execute(
                "SELECT category_key FROM notifications WHERE lot_id=?", (lot_id,)
            )
        )
        return LotStats(
            lot_id=lot_id,
            first_seen=row["first_seen"],
            first_bid=row["first_bid"],
            last_bid=row["last_bid"],
            notified=notified,
        )

    # -- prisadvarsler -----------------------------------------------------

    def feedback_actions(self, lot_ids: list[str]) -> dict[str, str]:
        """Handling pr. lot, for de lots brugeren selv har markeret."""
        if not lot_ids:
            return {}
        marks = ",".join("?" * len(lot_ids))
        rows = self.conn.execute(
            f"SELECT lot_id, action FROM feedback WHERE lot_id IN ({marks})", lot_ids
        ).fetchall()
        return {row["lot_id"]: row["action"] for row in rows}

    def price_alert_state(
        self, lot_ids: list[str]
    ) -> dict[str, tuple[int | None, str | None]]:
        """(sidst sete pris, hvornaar der sidst blev advaret) pr. lot."""
        if not lot_ids:
            return {}
        marks = ",".join("?" * len(lot_ids))
        rows = self.conn.execute(
            f"SELECT lot_id, cost, alerted_at FROM price_alerts WHERE lot_id IN ({marks})",
            lot_ids,
        ).fetchall()
        return {row["lot_id"]: (row["cost"], row["alerted_at"] or None) for row in rows}

    def set_price_baseline(self, lot_id: str, cost: int | None) -> None:
        """Opdatér den sete pris uden at roere advarselstidspunktet."""
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO price_alerts (lot_id, cost, alerted_at) VALUES (?,?, '')
                   ON CONFLICT(lot_id) DO UPDATE SET cost=excluded.cost""",
                (lot_id, cost),
            )

    def mark_price_alerted(self, lot_id: str, cost: int | None, alerted_at: str) -> None:
        """Gem at der er sendt besked, saa cooldown regnes derfra."""
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO price_alerts (lot_id, cost, alerted_at) VALUES (?,?,?)
                   ON CONFLICT(lot_id) DO UPDATE SET cost=excluded.cost,
                   alerted_at=excluded.alerted_at""",
                (lot_id, cost, alerted_at),
            )

    # -- sidste chance -----------------------------------------------------

    def last_chance_sent(self, lot_ids: list[str]) -> set[str]:
        """Hvilke af lot'ene har vi allerede advaret om."""
        if not lot_ids:
            return set()
        marks = ",".join("?" * len(lot_ids))
        rows = self.conn.execute(
            f"SELECT lot_id FROM last_chance_alerts WHERE lot_id IN ({marks})", lot_ids
        ).fetchall()
        return {row["lot_id"] for row in rows}

    def mark_last_chance(self, lot_ids: list[str], sent_at: str) -> None:
        if not lot_ids:
            return
        with self._tx() as conn:
            for lot_id in lot_ids:
                conn.execute(
                    "INSERT OR REPLACE INTO last_chance_alerts (lot_id, sent_at) VALUES (?,?)",
                    (lot_id, sent_at),
                )

    # -- AI-klassificering -------------------------------------------------

    def cached_classification(self, input_hash: str) -> dict | None:
        """Slå et tidligere klassificeringssvar op."""
        row = self.conn.execute(
            "SELECT verdict, reason, model FROM classifications WHERE input_hash=?",
            (input_hash,),
        ).fetchone()
        return dict(row) if row is not None else None

    def save_classification(
        self,
        input_hash: str,
        *,
        lot_id: str,
        category_key: str,
        version: str,
        model: str,
        verdict: str,
        reason: str = "",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO classifications
                   (input_hash, lot_id, category_key, version, model, verdict, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (input_hash, lot_id, category_key, version, model, verdict, reason, utcnow()),
            )

    # -- gennemsynskø ('måske') --------------------------------------------

    def enqueue_review(
        self,
        input_hash: str,
        *,
        lot_id: str,
        category_key: str,
        title: str,
        url: str,
        reason: str = "",
    ) -> None:
        """Læg et grænsetilfælde i kø til næste digest."""
        with self._tx() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO review_queue
                   (input_hash, lot_id, category_key, title, url, reason, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (input_hash, lot_id, category_key, title, url, reason, utcnow()),
            )

    def pending_reviews(self, limit: int = 25) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM review_queue WHERE digested_at IS NULL
               ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_review_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE digested_at IS NULL"
            ).fetchone()[0]
        )

    def mark_digested(self, input_hashes: list[str]) -> None:
        if not input_hashes:
            return
        now = utcnow()
        with self._tx() as conn:
            conn.executemany(
                "UPDATE review_queue SET digested_at=? WHERE input_hash=?",
                [(now, h) for h in input_hashes],
            )

    def classification_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM classifications GROUP BY verdict"
        ).fetchall()
        return {row["verdict"]: int(row["n"]) for row in rows}

    # -- vedligeholdelse ---------------------------------------------------

    def prune(self, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict[str, int]:
        """Ryd gammel historik. Returnerer antallet af slettede rækker pr. tabel.

        Kun historik ryddes. ``lots`` og ``notifications`` bevares, fordi de
        udgør selve hukommelsen: sletter man dem, gensender agenten gamle fund
        næste gang lot'et dukker op igen — eller mister evnen til at genkende
        det som set før.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(days=retention_days)
        ).isoformat(timespec="seconds")

        removed: dict[str, int] = {}
        with self._tx() as conn:
            # Lotter der ikke er set længe, er afsluttede. Deres pris-historik
            # er død vægt; 'lots'-rækken bliver for dedup'ens skyld.
            cur = conn.execute(
                """DELETE FROM price_history
                   WHERE observed_at < ?
                     AND lot_id IN (SELECT lot_id FROM lots WHERE last_seen < ?)""",
                (cutoff, cutoff),
            )
            removed["price_history"] = cur.rowcount

            cur = conn.execute(
                "DELETE FROM classifications WHERE created_at < ?", (cutoff,)
            )
            removed["classifications"] = cur.rowcount

            cur = conn.execute(
                """DELETE FROM review_queue
                   WHERE created_at < ? AND digested_at IS NOT NULL""",
                (cutoff,),
            )
            removed["review_queue"] = cur.rowcount

            cur = conn.execute(
                "DELETE FROM runs WHERE started_at < ? AND error IS NULL", (cutoff,)
            )
            removed["runs"] = cur.rowcount

        # WAL-filen tømmes uden for transaktionen — PRAGMA kan ikke køre inde i
        # en. Uden dette bliver WAL'en liggende som død vægt, fordi forbindelsen
        # aldrig lukkes i en proces der kører i månedsvis.
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {k: v for k, v in removed.items() if v}

    # -- meta --------------------------------------------------------------

    def record_details(self, lot_id: str, text: str, flags: tuple[str, ...]) -> None:
        """Gem hvad lot-siden sagde. Hentes én gang pr. lot."""
        with self._tx() as conn:
            conn.execute(
                """UPDATE lots SET details=?, details_flags=?, details_at=?
                   WHERE lot_id=?""",
                (text[:4000], ",".join(flags), utcnow(), lot_id),
            )

    def lots_without_details(self, lot_ids: list[str]) -> list[str]:
        """Hvilke af lot'ene mangler vi stadig en lot-side for?"""
        if not lot_ids:
            return []
        marks = ",".join("?" * len(lot_ids))
        rows = self.conn.execute(
            f"""SELECT lot_id FROM lots
                WHERE lot_id IN ({marks}) AND details_at = ''""",
            lot_ids,
        ).fetchall()
        return [row["lot_id"] for row in rows]

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
            )

    def last_successful_lot_count(self) -> int | None:
        """Antal lots i den seneste kørsel der ikke var tom.

        Bruges til at opdage at agenten er blevet blind: hvis hver kørsel nu
        giver 0 lots hvor den før gav hundreder, er der noget galt med
        udtrækket — ikke med udbuddet.
        """
        row = self.conn.execute(
            "SELECT lots FROM runs WHERE error IS NULL AND lots > 0 ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        return int(row["lots"]) if row else None

    # -- rapportering ------------------------------------------------------

    def counts(self) -> dict[str, int]:
        tables = ("lots", "price_history", "notifications", "runs",
                  "classifications", "review_queue")
        return {
            t: int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in tables
        }

    def export_json(self, path: str | Path) -> Path:
        """Dump hele hukommelsen til JSON, fx til backup eller videre analyse."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            table: [dict(r) for r in self.conn.execute(f"SELECT * FROM {table}")]
            for table in ("lots", "notifications", "runs")
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    def price_risers(self, limit: int = 20) -> list[dict]:
        """Lots hvor buddet er steget siden første gang vi så dem."""
        rows = self.conn.execute(
            """SELECT title, url, first_bid, last_bid, last_total
               FROM lots
               WHERE first_bid IS NOT NULL AND last_bid > first_bid
               ORDER BY (last_bid - first_bid) DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
