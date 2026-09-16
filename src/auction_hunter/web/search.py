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
from .queries import has_column, has_schema, has_table

PAGE_SIZE = 50

# Grænser for hvad et filter må indeholde. En formular kan sendes med hvad som
# helst, og et prisloft på 2^70 får sqlite3 til at kaste OverflowError mens et
# days_back på 740000 får datetime til at løbe tør for datoer. Begge dele blev
# til en 500 i stedet for en tom søgning.
MAX_PRICE = 100_000_000        # 100 mio. kr
MAX_DAYS_BACK = 3_650          # 10 år


def _positive(value: int | None, maximum: int) -> int | None:
    """Kun positive tal, og inden for hvad databasen og datetime kan klare."""
    if not value or value <= 0:
        return None
    return min(int(value), maximum)

# Hvad et lot kan filtreres på ud over fritekst.
STATUS_CHOICES = ("alle", "aktive", "afsluttede")
MATCH_CHOICES = ("alle", "kun_fund", "kun_ikke_fund")
SORT_CHOICES = ("relevans", "nyeste", "slutter", "pris_op", "pris_ned")
# Assistentens rangordninger. Ikke en søgning, men et spørgsmål om rækkefølge.
LISTE_CHOICES = ("", "stigere")


@dataclass(frozen=True)
class SearchQuery:
    """Et søgeopslag. Alle felter er valgfri."""

    text: str = ""
    min_price: int | None = None
    max_price: int | None = None
    status: str = "alle"
    matched: str = "alle"
    category: str = ""
    auction: str = ""
    days_back: int | None = None
    sort: str = "relevans"
    page: int = 1
    # Sand når ordene skal slås sammen med OR i stedet for AND. Bruges af
    # assistentens nøgleordsfald, hvor et enkelt dækord ellers dræber svaret.
    any_words: bool = False
    # Assistentens rangordning i stedet for en søgning. "stigere" beder om de
    # lots hvis bud er steget mest. Tom betyder en almindelig søgning.
    liste: str = ""

    def normalized(self) -> "SearchQuery":
        """Ret ugyldige værdier til deres standard i stedet for at fejle."""
        return SearchQuery(
            text=self.text.strip()[:200],
            min_price=_positive(self.min_price, MAX_PRICE),
            max_price=_positive(self.max_price, MAX_PRICE),
            status=self.status if self.status in STATUS_CHOICES else "alle",
            matched=self.matched if self.matched in MATCH_CHOICES else "alle",
            category=self.category.strip()[:64],
            auction=self.auction.strip()[:160],
            days_back=_positive(self.days_back, MAX_DAYS_BACK),
            sort=self.sort if self.sort in SORT_CHOICES else "relevans",
            page=max(1, self.page),
            any_words=self.any_words,
            liste=self.liste if self.liste in LISTE_CHOICES else "",
        )

    @property
    def is_empty(self) -> bool:
        return not any((
            self.text, self.min_price, self.max_price, self.category,
            self.auction, self.days_back, self.liste,
            self.status != "alle", self.matched != "alle",
        ))


@dataclass
class SearchResult:
    rows: list[sqlite3.Row] = field(default_factory=list)
    total: int = 0
    page: int = 1
    pages: int = 1
    query: SearchQuery = field(default_factory=SearchQuery)
    # Sand når søgningen ramte den hårde grænse. Så er total et gulv, ikke et
    # facit, og siden skal sige det i stedet for at påstå noget forkert.
    truncated: bool = False

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def to_fts_query(text: str, any_words: bool = False) -> str:
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
    # OR bruges af assistentens nøgleordsfald, hvor et enkelt dækord ikke må
    # kræve at alle de øvrige ord også står i titlen.
    return (" OR " if any_words else " AND ").join(clauses)


def search(conn: sqlite3.Connection, query: SearchQuery) -> SearchResult:
    """Kør en søgning mod arkivet."""
    query = query.normalized()

    if not has_schema(conn, "lots", "notifications"):
        return SearchResult(query=query)

    image = "l.image_url" if has_column(conn, "lots", "image_url") else "''"
    details_flags = ("l.details_flags" if has_column(conn, "lots", "details_flags")
                     else "'' AS details_flags")
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

    fts = to_fts_query(query.text, query.any_words)
    if fts:
        joins.insert(0, "JOIN lots_fts fts ON fts.lot_id = l.lot_id")
        where.append("lots_fts MATCH ?")
        params.append(fts)

    # Samme pris som kortet viser: den reelle total inkl. salær og moms.
    pris = "COALESCE(n.cost, l.last_total, l.last_bid, 0)"
    if query.min_price is not None:
        where.append(f"{pris} >= ?")
        params.append(query.min_price)
    if query.max_price is not None:
        where.append(f"{pris} <= ?")
        params.append(query.max_price)

    if query.category:
        where.append("n.category_key = ?")
        params.append(query.category)

    # "Hvor kommer det fra" er auktionen. Auktionshusets auktionstitler bærer
    # både sælger og sted, fx "Auktion Køge".
    if query.auction:
        where.append("l.auction_title = ?")
        params.append(query.auction)

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
        "pris_op": "COALESCE(n.cost, l.last_total, l.last_bid, 0) ASC",
        "pris_ned": "COALESCE(n.cost, l.last_total, l.last_bid, 0) DESC",
    }[query.sort]

    select = f"""
        SELECT DISTINCT l.lot_id, l.title, l.url, l.auction_title, l.lot_number,
               l.first_seen, l.last_seen, l.ends_at,
               l.first_bid, l.last_bid, l.last_total, {details_flags},
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

    truncated = len(rows) >= hard_limit
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
        truncated=truncated,
    )


def archive_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Overblik over hvor stort arkivet er."""
    if not has_schema(conn, "lots", "notifications"):
        return {"total": 0, "matched": 0, "unmatched": 0, "indexed": 0}
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
    if not has_table(conn, "notifications"):
        return []
    return [
        row[0] for row in conn.execute(
            "SELECT DISTINCT category_key FROM notifications "
            "WHERE category_key <> '' ORDER BY category_key"
        )
    ]
