"""Fritekstsøgning over samtlige sete lots — ikke kun dem der matchede.

Agenten gemmer hvert lot den ser (``record_lots(all_lots)``), så arkivet
indeholder ~2.200 lots pr. kørsel, hvoraf en håndfuld bliver til fund. Det er
det arkiv man leder i, når man vil vide om noget har været til salg, uden at
det nogensinde ramte interesseprofilen.

Søgningen går mod ``lots_fts``, hvor titlen ligger både rå og normaliseret, så
``hojttaler`` finder ``højttaler``.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..textmatch import normalize, normalize_loose
from .formatting import parse_dt
from .queries import has_column, has_table

PAGE_SIZE = 50

# Hvad et lot kan filtreres på ud over fritekst.
STATUS_CHOICES = ("alle", "aktive", "afsluttede")
MATCH_CHOICES = ("alle", "kun_fund", "kun_ikke_fund")
SORT_CHOICES = ("relevans", "nyeste", "slutter", "pris_op", "pris_ned")


@dataclass(frozen=True)
class SearchQuery:
    """Et søgeopslag. Alle felter er valgfri."""

    text: str = ""
    min_price: int | None = None
    max_price: int | None = None
    status: str = "alle"
    matched: str = "alle"
    category: str = ""
    days_back: int | None = None
    sort: str = "relevans"
    page: int = 1

    def normalized(self) -> "SearchQuery":
        """Ret ugyldige værdier til deres standard i stedet for at fejle."""
        return SearchQuery(
            text=self.text.strip()[:200],
            min_price=self.min_price if (self.min_price or 0) > 0 else None,
            max_price=self.max_price if (self.max_price or 0) > 0 else None,
            status=self.status if self.status in STATUS_CHOICES else "alle",
            matched=self.matched if self.matched in MATCH_CHOICES else "alle",
            category=self.category.strip()[:64],
            days_back=self.days_back if (self.days_back or 0) > 0 else None,
            sort=self.sort if self.sort in SORT_CHOICES else "relevans",
            page=max(1, self.page),
        )

    @property
    def is_empty(self) -> bool:
        return not any((
            self.text, self.min_price, self.max_price, self.category,
            self.days_back, self.status != "alle", self.matched != "alle",
        ))


@dataclass
class SearchResult:
    rows: list[sqlite3.Row] = field(default_factory=list)
    total: int = 0
    page: int = 1
    pages: int = 1
    query: SearchQuery = field(default_factory=SearchQuery)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def to_fts_query(text: str) -> str:
    """Byg et sikkert FTS5-udtryk af brugerens ord.

    FTS5 har sin egen syntaks hvor ``"``, ``*``, ``:``, ``^`` og ``NEAR`` har
    betydning. Vi citerer derfor hvert ord, så en søgning på ``NAS "8 bay"``
    ikke bliver til et syntaksfejl. Hvert ord søges både råt og normaliseret.
    """
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if not words:
        return ""

    clauses = []
    for word in words:
        # Fjern citationstegn; de er den eneste vej til at bryde ud af citatet.
        raw = word.replace('"', "")
        if not raw:
            continue
        # Begge danske foldninger, så både 'hoejttaler' og 'hojttaler' rammer.
        variants = {raw.lower(), normalize(raw), normalize_loose(raw)} - {""}
        # Præfiks-match, så 'højttal' finder 'højttaler'.
        parts = " OR ".join(f'"{v}"*' for v in sorted(variants))
        clauses.append(f"({parts})")
    return " AND ".join(clauses)


def search(conn: sqlite3.Connection, query: SearchQuery) -> SearchResult:
    """Kør en søgning mod arkivet."""
    query = query.normalized()

    image = "l.image_url" if has_column(conn, "lots", "image_url") else "''"
    feedback_join = (
        "LEFT JOIN feedback f ON f.lot_id = l.lot_id"
        if has_table(conn, "feedback") else ""
    )
    feedback_col = "COALESCE(f.action,'')" if feedback_join else "''"

    joins = [
        "LEFT JOIN notifications n ON n.lot_id = l.lot_id",
        feedback_join,
    ]
    where: list[str] = []
    params: list[object] = []

    fts = to_fts_query(query.text)
    if fts:
        joins.insert(0, "JOIN lots_fts fts ON fts.lot_id = l.lot_id")
        where.append("lots_fts MATCH ?")
        params.append(fts)

    if query.min_price is not None:
        where.append("COALESCE(l.last_total, l.last_bid, 0) >= ?")
        params.append(query.min_price)
    if query.max_price is not None:
        where.append("COALESCE(l.last_total, l.last_bid, 0) <= ?")
        params.append(query.max_price)

    if query.category:
        where.append("n.category_key = ?")
        params.append(query.category)

    if query.matched == "kun_fund":
        where.append("n.lot_id IS NOT NULL")
    elif query.matched == "kun_ikke_fund":
        where.append("n.lot_id IS NULL")

    if query.days_back:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=query.days_back)
        ).isoformat(timespec="seconds")
        where.append("l.first_seen >= ?")
        params.append(cutoff)

    # 'aktive' og 'afsluttede' kan ikke afgøres i SQL, fordi ends_at er ISO med
    # offset. Vi henter derfor bredt og filtrerer i Python nedenfor.
    needs_python_status = query.status in ("aktive", "afsluttede")

    join_sql = " ".join(j for j in joins if j)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    order = {
        "relevans": "fts.rank" if fts else "l.last_seen DESC",
        "nyeste": "l.first_seen DESC",
        "slutter": "l.ends_at IS NULL, l.ends_at ASC",
        "pris_op": "COALESCE(l.last_total, l.last_bid, 0) ASC",
        "pris_ned": "COALESCE(l.last_total, l.last_bid, 0) DESC",
    }[query.sort]

    select = f"""
        SELECT DISTINCT l.lot_id, l.title, l.url, l.auction_title, l.lot_number,
               l.first_seen, l.last_seen, l.ends_at,
               l.first_bid, l.last_bid, l.last_total,
               {image} AS image_url,
               n.category_key, n.sent_at, n.cost,
               {feedback_col} AS feedback_action,
               CASE WHEN n.lot_id IS NULL THEN 0 ELSE 1 END AS was_match
        FROM lots l
        {join_sql}
        {where_sql}
        ORDER BY {order}
    """

    # Ved statusfiltrering henter vi mere end en side og skærer bagefter, så
    # pagineringen stadig passer. Grænsen holder hukommelsen i ro på en stor
    # database.
    hard_limit = 4000 if needs_python_status else 2000
    rows = conn.execute(select + " LIMIT ?", (*params, hard_limit)).fetchall()

    if needs_python_status:
        now = datetime.now(timezone.utc)
        def active(row: sqlite3.Row) -> bool:
            ends = parse_dt(row["ends_at"])
            return ends is None or ends > now
        rows = [r for r in rows if active(r) == (query.status == "aktive")]

    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(query.page, pages)
    start = (page - 1) * PAGE_SIZE

    return SearchResult(
        rows=rows[start:start + PAGE_SIZE],
        total=total,
        page=page,
        pages=pages,
        query=query,
    )


def archive_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Overblik over hvor stort arkivet er."""
    total = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    matched = conn.execute(
        "SELECT COUNT(DISTINCT lot_id) FROM notifications"
    ).fetchone()[0]
    indexed = (
        conn.execute("SELECT COUNT(*) FROM lots_fts").fetchone()[0]
        if has_table(conn, "lots_fts") else 0
    )
    return {
        "total": total,
        "matched": matched,
        "unmatched": max(0, total - matched),
        "indexed": indexed,
    }


def categories_seen(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0] for row in conn.execute(
            "SELECT DISTINCT category_key FROM notifications "
            "WHERE category_key <> '' ORDER BY category_key"
        )
    ]
