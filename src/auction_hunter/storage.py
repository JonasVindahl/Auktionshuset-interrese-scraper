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

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .matcher import Match
from .scraper import Lot

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
    ends_at       TEXT
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
    sent_at      TEXT NOT NULL,
    cost         INTEGER,
    PRIMARY KEY (lot_id, category_key)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    auctions      INTEGER NOT NULL DEFAULT 0,
    lots          INTEGER NOT NULL DEFAULT 0,
    matches       INTEGER NOT NULL DEFAULT 0,
    new_matches   INTEGER NOT NULL DEFAULT 0,
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
"""

# Hvor længe historik holdes. Auktioner løber i uger, så et halvt år er rigeligt
# til at se et mønster, og grænsen holder databasen fra at vokse i det uendelige
# — uden den ville pris-historikken alene blive flere GB om året.
DEFAULT_RETENTION_DAYS = 180


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utcnow_precise() -> str:
    """Tidsstempel med mikrosekunder, til pris-historikken.

    Primærnøglen i ``price_history`` er ``(lot_id, observed_at)``. Med kun
    sekund-præcision ville et lot hvis pris ændrede sig to gange inden for samme
    sekund — hvilket sker når en tests kører hurtigt, og når en auktion lukker
    med bud i sidste øjeblik — kun gemme den sidste observation.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

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

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- kørsler -----------------------------------------------------------

    def start_run(self) -> int:
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
        error: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """UPDATE runs SET finished_at=?, auctions=?, lots=?, matches=?,
                   new_matches=?, error=? WHERE run_id=?""",
                (utcnow(), auctions, lots, matches, new_matches, error, run_id),
            )

    # -- lots --------------------------------------------------------------

    def record_lot(self, lot: Lot, total: int | None) -> None:
        """Gem lot og dets aktuelle pris; opdater pris-historik ved ændring."""
        now = utcnow()
        ends = lot.ends_at.isoformat(timespec="seconds") if lot.ends_at else None

        with self._tx() as conn:
            row = conn.execute(
                "SELECT last_bid, last_total FROM lots WHERE lot_id=?", (lot.lot_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO lots (lot_id, auction_id, auction_title, title, url,
                       lot_number, first_seen, last_seen, first_bid, last_bid, last_total, ends_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        lot.lot_id, lot.auction_id, lot.auction_title, lot.title, lot.url,
                        lot.lot_number, now, now, lot.current_bid, lot.current_bid, total, ends,
                    ),
                )
                price_changed = True
            else:
                conn.execute(
                    """UPDATE lots SET last_seen=?, last_bid=?, last_total=?, ends_at=?,
                       title=?, url=? WHERE lot_id=?""",
                    (now, lot.current_bid, total, ends, lot.title, lot.url, lot.lot_id),
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

    def record_lots(self, lots: list[Lot], totals: dict[str, int | None] | None = None) -> None:
        totals = totals or {}
        for lot in lots:
            self.record_lot(lot, totals.get(lot.lot_id))

    # -- notifikationer ----------------------------------------------------

    def already_notified(self, lot_id: str, category_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM notifications WHERE lot_id=? AND category_key=?",
            (lot_id, category_key),
        ).fetchone()
        return row is not None

    def mark_notified(self, lot_id: str, category_key: str, cost: int | None) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO notifications (lot_id, category_key, sent_at, cost)
                   VALUES (?,?,?,?)""",
                (lot_id, category_key, utcnow(), cost),
            )

    def filter_new(self, matches: list[Match]) -> list[Match]:
        """Behold kun fund der ikke er sendt før."""
        return [
            m for m in matches
            if not self.already_notified(m.lot.lot_id, m.category.key)
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
            datetime.now(timezone.utc) - timedelta(days=retention_days)
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
