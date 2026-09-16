"""Databaseopslag til webdashboardet.

Alt her læser. Den eneste skrivning fra webben er feedback-markeringer, som
ligger i ``save_feedback``. Agenten ejer alt andet.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .formatting import parse_dt

# Hvor langt tilbage "udløbet"-siden kigger.
EXPIRED_WINDOW_HOURS = 48

# Gyldige feedback-handlinger. Alt andet afvises.
ACTIONS = ("skip", "bid", "bought")


def ro_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rw_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def has_schema(conn: sqlite3.Connection, *tables: str) -> bool:
    """Findes alle de nævnte tabeller?

    En databasefil uden tabeller er et helt normalt første kørselsscenarie:
    dashboardet kan startes før agenten har skrevet noget. Siderne skal vise en
    tom tilstand, ikke en 500.
    """
    return all(has_table(conn, table) for table in tables)


# -- fund ------------------------------------------------------------------

def notifications(conn: sqlite3.Connection, limit: int = 400) -> list[sqlite3.Row]:
    """Sendte fund, nyeste først, med lot-detaljer og eventuel markering."""
    if not has_schema(conn, "lots", "notifications"):
        return []
    image = "l.image_url" if has_column(conn, "lots", "image_url") else "'' AS image_url"
    return conn.execute(
        f"""
        SELECT n.lot_id, n.category_key, n.sent_at, n.cost,
               l.title, l.url, l.auction_title, l.ends_at, l.lot_number,
               l.first_bid, l.last_bid, {image},
               COALESCE(f.action, '') AS feedback_action
        FROM notifications n
        JOIN lots l ON n.lot_id = l.lot_id
        LEFT JOIN feedback f
               ON f.lot_id = n.lot_id AND f.category_key = n.category_key
        ORDER BY n.sent_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def split_by_end(rows: list[sqlite3.Row]) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Del i (aktive, netop udløbne).

    Filtreringen sker i Python, fordi ``ends_at`` ikke kan sammenlignes som
    streng med SQLites tidsfunktioner — se ``formatting``.
    """
    now = datetime.now(timezone.utc)
    active: list[sqlite3.Row] = []
    expired: list[sqlite3.Row] = []
    for row in rows:
        ends = parse_dt(row["ends_at"])
        if ends is None or ends > now:
            active.append(row)
            continue
        if (now - ends) <= timedelta(hours=EXPIRED_WINDOW_HOURS):
            expired.append(row)
    expired.sort(key=lambda r: parse_dt(r["ends_at"]) or now, reverse=True)
    return active, expired


def pending_reviews(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    if not has_table(conn, "review_queue"):
        return []
    return conn.execute(
        """
        SELECT title, url, reason, created_at
        FROM review_queue
        WHERE digested_at IS NULL
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


# -- tal til forsiden ------------------------------------------------------

def counts(conn: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in ("lots", "notifications"):
        result[table] = (
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if has_table(conn, table) else 0
        )
    result["pending_review"] = (
        conn.execute(
            "SELECT COUNT(*) FROM review_queue WHERE digested_at IS NULL"
        ).fetchone()[0]
        if has_table(conn, "review_queue") else 0
    )
    result["marked"] = (
        conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        if has_table(conn, "feedback") else 0
    )
    return result


def last_run(conn: sqlite3.Connection) -> tuple[str, bool]:
    """(tekst, er_forældet). Forældet = mere end 45 minutter siden."""
    if not has_table(conn, "runs"):
        return "ingen kørsel endnu", True
    row = conn.execute(
        "SELECT finished_at FROM runs WHERE finished_at IS NOT NULL "
        "ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return "ingen kørsel endnu", True
    dt = parse_dt(row[0])
    if dt is None:
        return str(row[0]), True
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    stale = mins > 45
    if mins < 2:
        return "opdateret lige nu", stale
    if mins < 60:
        return f"opdateret for {mins} min. siden", stale
    return f"opdateret for {mins // 60} t. siden", stale


def feedback_breakdown(conn: sqlite3.Connection) -> dict[str, int]:
    if not has_table(conn, "feedback"):
        return {}
    return {
        row["action"]: row["n"]
        for row in conn.execute(
            "SELECT action, COUNT(*) AS n FROM feedback GROUP BY action"
        )
    }


def category_breakdown(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Hvor mange fund pr. kategori, og hvor mange der blev markeret som støj."""
    if not has_schema(conn, "notifications", "feedback"):
        return []
    rows = conn.execute(
        """
        SELECT n.category_key AS category,
               COUNT(*) AS sent,
               SUM(CASE WHEN f.action = 'skip'   THEN 1 ELSE 0 END) AS skipped,
               SUM(CASE WHEN f.action = 'bought' THEN 1 ELSE 0 END) AS bought,
               SUM(CASE WHEN f.action = 'bid'    THEN 1 ELSE 0 END) AS bid
        FROM notifications n
        LEFT JOIN feedback f
               ON f.lot_id = n.lot_id AND f.category_key = n.category_key
        GROUP BY n.category_key
        ORDER BY sent DESC
        """
    ).fetchall()
    out = []
    for row in rows:
        sent = row["sent"] or 0
        skipped = row["skipped"] or 0
        out.append({
            "category": row["category"],
            "sent": sent,
            "skipped": skipped,
            "bought": row["bought"] or 0,
            "bid": row["bid"] or 0,
            # Andelen du har afvist er det bedste mål for om kategorien støjer.
            "noise_pct": round(100 * skipped / sent) if sent else 0,
        })
    return out


def run_history(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    if not has_table(conn, "runs"):
        return []
    return conn.execute(
        """
        SELECT run_id, started_at, finished_at, auctions, lots,
               matches, new_matches, error
        FROM runs
        WHERE finished_at IS NOT NULL
        ORDER BY run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def price_risers(conn: sqlite3.Connection, limit: int = 15) -> list[sqlite3.Row]:
    if not has_table(conn, "lots"):
        return []
    return conn.execute(
        """
        SELECT l.title, l.url, l.first_bid, l.last_bid, l.ends_at
        FROM lots l
        WHERE l.first_bid IS NOT NULL AND l.last_bid > l.first_bid
        ORDER BY (l.last_bid - l.first_bid) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


# -- markeringer -----------------------------------------------------------

def save_feedback(
    db_path: str, lot_id: str, category_key: str, action: str, title: str
) -> None:
    """Gem eller fjern en markering. Tom ``action`` sletter rækken."""
    if action and action not in ACTIONS:
        raise ValueError(f"ukendt handling: {action!r}")
    conn = rw_conn(db_path)
    try:
        if action:
            conn.execute(
                """INSERT OR REPLACE INTO feedback
                   (lot_id, category_key, action, title, created_at)
                   VALUES (?,?,?,?,?)""",
                (lot_id, category_key, action, title,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        else:
            conn.execute(
                "DELETE FROM feedback WHERE lot_id=? AND category_key=?",
                (lot_id, category_key),
            )
        conn.commit()
    finally:
        conn.close()


def feedback_export(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Alle markeringer, nyeste først — træningsdata."""
    if not has_table(conn, "feedback"):
        return []
    return conn.execute(
        """
        SELECT f.lot_id, f.category_key, f.action, f.title, f.created_at,
               l.url, l.last_total
        FROM feedback f
        LEFT JOIN lots l ON l.lot_id = f.lot_id
        ORDER BY f.created_at DESC
        """
    ).fetchall()
