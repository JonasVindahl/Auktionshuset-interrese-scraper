"""Databaseopslag til webdashboardet.

Alt her læser. Den eneste skrivning fra webben er feedback-markeringer, som
ligger i ``save_feedback``. Agenten ejer alt andet.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from ..textmatch import find_keywords
from .formatting import parse_dt

# Hvor langt tilbage "udløbet"-siden kigger.
EXPIRED_WINDOW_HOURS = 48

# Gyldige feedback-handlinger. Alt andet afvises.
# Handlinger brugeren kan sætte på et lot. "watch" er ikke en afgørelse, men
# en påmindelse om at lot'et skal følges — den hører hjemme her fordi det er
# samme slags menneskelige svar som de andre.
ACTIONS = ("skip", "watch", "bid", "bought")


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
    # Kolonner fra migrationer må ikke kunne vælte siden: web-containeren kan
    # starte før agenten har kørt sin første migration.
    flags = ("l.details_flags" if has_column(conn, "lots", "details_flags")
             else "'' AS details_flags")
    # Samme forsigtighed som ovenfor: profile_key kom med profilerne, og
    # web-containeren kan starte foer agenten har migreret.
    profile = ("n.profile_key" if has_column(conn, "notifications", "profile_key")
               else "'standard' AS profile_key")
    return conn.execute(
        f"""
        SELECT n.lot_id, n.category_key, {profile}, n.sent_at, n.cost,
               l.title, l.url, l.auction_title, l.ends_at, l.lot_number,
               l.first_bid, l.last_bid, l.last_total, {flags}, {image},
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
    now = datetime.now(UTC)
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
        SELECT lot_id, category_key, title, url, reason, created_at
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
    mins = int((datetime.now(UTC) - dt).total_seconds() // 60)
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
                 datetime.now(UTC).isoformat(timespec="seconds")),
            )
        else:
            conn.execute(
                "DELETE FROM feedback WHERE lot_id=? AND category_key=?",
                (lot_id, category_key),
            )
        conn.commit()
    finally:
        conn.close()


def lot_detail(conn: sqlite3.Connection, lot_id: str) -> sqlite3.Row | None:
    """Alt om ét lot: rækken, dens notifikation og din markering."""
    if not has_schema(conn, "lots"):
        return None
    image = "l.image_url" if has_column(conn, "lots", "image_url") else "'' AS image_url"
    details = ("l.details" if has_column(conn, "lots", "details")
               else "'' AS details")
    details_flags = ("l.details_flags" if has_column(conn, "lots", "details_flags")
                     else "'' AS details_flags")
    notif = (
        "LEFT JOIN notifications n ON n.lot_id = l.lot_id"
        if has_table(conn, "notifications") else ""
    )
    feedback_join = (
        "LEFT JOIN feedback f ON f.lot_id = l.lot_id"
        if has_table(conn, "feedback") else ""
    )
    feedback = (
        "COALESCE(f.action,'') AS feedback_action"
        if has_table(conn, "feedback") else "'' AS feedback_action"
    )
    sent_at = "n.sent_at" if has_table(conn, "notifications") else "NULL AS sent_at"
    cost = "n.cost" if has_table(conn, "notifications") else "NULL AS cost"
    category = (
        "n.category_key" if has_table(conn, "notifications") else "'' AS category_key"
    )
    return conn.execute(
        f"""
        SELECT l.lot_id, l.title, l.url, l.auction_title, l.lot_number,
               l.first_seen, l.last_seen, l.first_bid, l.last_bid, l.last_total,
               l.ends_at, {details}, {details_flags},
               {image}, {category}, {sent_at}, {cost}, {feedback}
        FROM lots l
        {notif}
        {feedback_join}
        WHERE l.lot_id = ?
        ORDER BY n.sent_at DESC
        LIMIT 1
        """,
        (lot_id,),
    ).fetchone()


def lots_by_ids(conn: sqlite3.Connection, lot_ids: list[str]) -> list[sqlite3.Row]:
    """Samme raekkeform som soegningen, men for et bestemt saet lot_id'er.

    Bruges naar en gemt assistent-besked skal vise sine kort igen: beskeden
    gemmer lot_id'erne, ikke et oejebliksbillede af priserne.
    """
    if not lot_ids or not has_schema(conn, "lots", "notifications"):
        return []
    image = "l.image_url" if has_column(conn, "lots", "image_url") else "''"
    details_flags = ("l.details_flags" if has_column(conn, "lots", "details_flags")
                     else "'' AS details_flags")
    feedback_join = (
        "LEFT JOIN feedback f ON f.lot_id = l.lot_id"
        if has_table(conn, "feedback") else ""
    )
    feedback_col = "COALESCE(f.action,'')" if feedback_join else "''"
    marks = ",".join("?" * len(lot_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT l.lot_id, l.title, l.url, l.auction_title, l.lot_number,
               l.first_seen, l.last_seen, l.ends_at,
               l.first_bid, l.last_bid, l.last_total, {details_flags},
               {image} AS image_url,
               n.category_key, n.sent_at, n.cost,
               {feedback_col} AS feedback_action,
               CASE WHEN n.lot_id IS NULL THEN 0 ELSE 1 END AS was_match
        FROM lots l
        LEFT JOIN notifications n ON n.lot_id = l.lot_id
        {feedback_join}
        WHERE l.lot_id IN ({marks})
        """,
        list(lot_ids),
    ).fetchall()
    by_id = {row["lot_id"]: row for row in rows}
    return [by_id[lot_id] for lot_id in lot_ids if lot_id in by_id]


def risers(conn: sqlite3.Connection, limit: int = 12) -> list[sqlite3.Row]:
    """Lots hvor buddet er steget mest siden første registrering.

    Samme raekkeform som soegningen, saa kortene kan tegnes som de andre.
    """
    if not has_schema(conn, "lots", "notifications"):
        return []
    image = "l.image_url" if has_column(conn, "lots", "image_url") else "''"
    details_flags = ("l.details_flags" if has_column(conn, "lots", "details_flags")
                     else "'' AS details_flags")
    feedback_join = (
        "LEFT JOIN feedback f ON f.lot_id = l.lot_id"
        if has_table(conn, "feedback") else ""
    )
    feedback_col = "COALESCE(f.action,'')" if feedback_join else "''"
    return conn.execute(
        f"""
        SELECT DISTINCT l.lot_id, l.title, l.url, l.auction_title, l.lot_number,
               l.first_seen, l.last_seen, l.ends_at,
               l.first_bid, l.last_bid, l.last_total, {details_flags},
               {image} AS image_url,
               n.category_key, n.sent_at, n.cost,
               {feedback_col} AS feedback_action,
               CASE WHEN n.lot_id IS NULL THEN 0 ELSE 1 END AS was_match
        FROM lots l
        LEFT JOIN notifications n ON n.lot_id = l.lot_id
        {feedback_join}
        WHERE l.first_bid IS NOT NULL AND l.last_bid IS NOT NULL
          AND l.last_bid > l.first_bid
        ORDER BY (l.last_bid - l.first_bid) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rækkeantal pr. tabel. Driftssiden skal kunne se hvad der vokser."""
    counts: dict[str, int] = {}
    for table in ("lots", "price_history", "notifications", "runs",
                  "classifications", "review_queue", "feedback", "meta"):
        if has_table(conn, table):
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


def memory_span(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Hvornår hukommelsen begynder og slutter."""
    if not has_schema(conn, "lots"):
        return {"first": None, "last": None}
    row = conn.execute(
        "SELECT MIN(first_seen) AS a, MAX(last_seen) AS b FROM lots"
    ).fetchone()
    return {"first": row["a"], "last": row["b"]}


def meta_value(conn: sqlite3.Connection, key: str) -> str | None:
    if not has_table(conn, "meta"):
        return None
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def last_normal_lot_count(conn: sqlite3.Connection) -> int | None:
    """Antal lots i den seneste kørsel der ikke var tom. Blindheds-tærsklen."""
    if not has_table(conn, "runs"):
        return None
    row = conn.execute(
        "SELECT lots FROM runs WHERE error IS NULL AND lots > 0 ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return int(row["lots"]) if row else None


def price_series(conn: sqlite3.Connection, lot_ids: list[str]) -> dict[str, list[int]]:
    """Prisforløb pr. lot, ældste observation først.

    Ét opslag for alle viste lots frem for ét pr. kort, ellers ville en side
    med 50 kort give 50 forespørgsler. Kun faktiske prisændringer gemmes, så
    serien er kort — derfor kan den tegnes direkte.
    """
    if not lot_ids or not has_table(conn, "price_history"):
        return {}
    marks = ",".join("?" * len(lot_ids))
    rows = conn.execute(
        f"""SELECT lot_id, COALESCE(total, bid) AS pris FROM price_history
            WHERE lot_id IN ({marks}) ORDER BY lot_id, observed_at""",
        lot_ids,
    ).fetchall()
    out: dict[str, list[int]] = {}
    for row in rows:
        if row["pris"]:
            out.setdefault(row["lot_id"], []).append(int(row["pris"]))
    return out


def auctions_seen(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Hvilke auktioner arkivet dækker, med antal lots. Bruges til at filtrere
    på hvor et lot kommer fra."""
    if not has_schema(conn, "lots"):
        return []
    return [
        {"title": row["auction_title"], "n": row["n"]}
        for row in conn.execute(
            """SELECT auction_title, COUNT(*) AS n FROM lots
               WHERE auction_title <> '' GROUP BY auction_title
               ORDER BY n DESC, auction_title"""
        ).fetchall()
    ]


def keyword_noise(
    conn: sqlite3.Connection, config: Any, *, min_seen: int = 3, limit: int = 8
) -> list[dict[str, Any]]:
    """Hvilke nøgleord trækker støj ind?

    For hvert lot du har fået sendt, findes de nøgleord der udløste det, og
    andelen af dem du har afvist. Et ord der ofte optræder i afviste lots er
    for bredt. Det er et fingerpeg, ikke et bevis: ordet kan være uskyldigt i
    netop de titler, så tallene vises sammen med antallet.
    """
    if config is None or not has_schema(conn, "notifications", "lots", "feedback"):
        return []
    rows = conn.execute(
        """
        SELECT n.category_key, COALESCE(f.action, '') AS action, l.title
        FROM notifications n
        JOIN lots l ON l.lot_id = n.lot_id
        LEFT JOIN feedback f
               ON f.lot_id = n.lot_id AND f.category_key = n.category_key
        """
    ).fetchall()

    stats: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        category = config.category(row["category_key"])
        if category is None:
            continue
        hits = find_keywords(
            row["title"] or "",
            category.strong + category.weak + category.brands,
            exact=category.exact,
        )
        for keyword in hits:
            entry = stats.setdefault((row["category_key"], keyword), {"seen": 0, "skip": 0})
            entry["seen"] += 1
            if row["action"] == "skip":
                entry["skip"] += 1

    out = [
        {
            "category": category,
            "keyword": keyword,
            "seen": entry["seen"],
            "skip": entry["skip"],
            "pct": round(100 * entry["skip"] / entry["seen"]),
        }
        for (category, keyword), entry in stats.items()
        if entry["seen"] >= min_seen and entry["skip"] > 0
    ]
    out.sort(key=lambda row: (-row["pct"], -row["seen"], row["keyword"]))
    return out[:limit]


def archive_export(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Hele arkivet, nyeste først. Til regneark og videre analyse."""
    if not has_schema(conn, "lots"):
        return []
    return conn.execute(
        """
        SELECT l.lot_id, l.title, l.url, l.auction_title, l.lot_number,
               l.first_seen, l.last_seen, l.first_bid, l.last_bid, l.last_total,
               l.ends_at,
               COALESCE(n.category_key, '') AS category_key,
               COALESCE(n.sent_at, '') AS sent_at
        FROM lots l
        LEFT JOIN notifications n ON n.lot_id = l.lot_id
        ORDER BY l.first_seen DESC
        """
    ).fetchall()


def marked_lots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Lots du selv har markeret. Det er den eneste skrivning webben ejer, og
    indtil nu var der ingen side der viste den igen."""
    if not has_schema(conn, "feedback", "lots"):
        return []
    image = "l.image_url" if has_column(conn, "lots", "image_url") else "'' AS image_url"
    return conn.execute(
        f"""
        SELECT f.lot_id, f.category_key, f.action AS feedback_action, f.created_at,
               l.title, l.url, l.auction_title, l.ends_at, l.lot_number,
               l.first_bid, l.last_bid, l.last_total, {image}
        FROM feedback f
        JOIN lots l ON l.lot_id = f.lot_id
        ORDER BY f.created_at DESC
        """
    ).fetchall()


def price_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Hvad fundene reelt koster, pr. kategori.

    Prisen er totalen inkl. salær og moms, altså det man faktisk betaler. Et
    lot uden bud har ingen total, så det tælles separat frem for at indgå som
    nul og trække gennemsnittet ned.
    """
    if not has_schema(conn, "notifications", "lots"):
        return []
    rows = conn.execute(
        """
        SELECT n.category_key AS category,
               COUNT(*) AS sent,
               SUM(CASE WHEN COALESCE(n.cost, l.last_total, l.last_bid) IS NULL
                        THEN 1 ELSE 0 END) AS uden_bud,
               MIN(COALESCE(n.cost, l.last_total, l.last_bid)) AS min_pris,
               MAX(COALESCE(n.cost, l.last_total, l.last_bid)) AS max_pris,
               AVG(COALESCE(n.cost, l.last_total, l.last_bid)) AS gns_pris
        FROM notifications n
        JOIN lots l ON l.lot_id = n.lot_id
        GROUP BY n.category_key
        ORDER BY sent DESC
        """
    ).fetchall()
    return [
        {
            "category": row["category"],
            "sent": row["sent"] or 0,
            "uden_bud": row["uden_bud"] or 0,
            "min": row["min_pris"],
            "max": row["max_pris"],
            "gns": round(row["gns_pris"]) if row["gns_pris"] is not None else None,
        }
        for row in rows
    ]


def price_bands(
    conn: sqlite3.Connection, edges: tuple[int, ...] = (500, 1000, 2500, 5000)
) -> list[dict[str, Any]]:
    """Hvor mange fund ligger i hvert prisleje. Sidste bånd er åbent opad.

    Prisen er den reelle total inkl. salær og moms. Lots uden bud har ingen
    total og udelades, så de ikke trækker billedet mod nul.
    """
    if not has_schema(conn, "notifications", "lots"):
        return []
    bands: list[dict[str, Any]] = [
        {"label": f"under {edges[0]} kr", "lo": None, "hi": edges[0], "n": 0}
    ]
    for lo, hi in zip(edges, edges[1:], strict=False):
        bands.append({"label": f"{lo}-{hi} kr", "lo": lo, "hi": hi, "n": 0})
    bands.append({"label": f"over {edges[-1]} kr", "lo": edges[-1], "hi": None, "n": 0})

    rows = conn.execute(
        """
        SELECT COALESCE(n.cost, l.last_total, l.last_bid) AS pris
        FROM notifications n JOIN lots l ON l.lot_id = n.lot_id
        WHERE COALESCE(n.cost, l.last_total, l.last_bid) IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        pris = row["pris"]
        for band in bands:
            if ((band["lo"] is None or pris >= band["lo"])
                    and (band["hi"] is None or pris < band["hi"])):
                band["n"] += 1
                break
    return bands


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
