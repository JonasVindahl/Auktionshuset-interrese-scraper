"""Simpelt read-only webdashboard over sendte fund og review-køen.

Kører som en separat proces ved siden af scraperen og læser samme SQLite-fil.
SQLite WAL-tilstand gør at læsning ikke blokerer skrivning, og omvendt.

    python -m auction_hunter web [--port 8080] [--host 0.0.0.0]
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# CSS — adskilt fra Python-strenge der bruger .format() eller f-strings
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg:#f5f5f3;--surface:#fff;--border:#e4e4de;--text:#1a1a18;
  --muted:#6b6b65;--accent:#2a7d4f;--over:#b45309;--maybe:#4f46e5;
  --tag-bg:#eef2eb;--tag-text:#2a7d4f;--radius:10px;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 2px 8px rgba(0,0,0,.04);
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#111110;--surface:#1c1c1a;--border:#2c2c28;--text:#e8e8e2;
    --muted:#808078;--accent:#4ade80;--over:#fbbf24;--maybe:#818cf8;
    --tag-bg:#1a2e20;--tag-text:#4ade80;
    --shadow:0 1px 3px rgba(0,0,0,.3),0 2px 8px rgba(0,0,0,.2);
  }
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
a{color:inherit;text-decoration:none}

header{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:12px 20px;display:flex;align-items:center;gap:10px;
  position:sticky;top:0;z-index:20;
}
header h1{font-size:.95rem;font-weight:700;letter-spacing:-.01em}
header .sub{color:var(--muted);font-size:.8rem;margin-left:auto}

.stats-bar{display:flex;background:var(--surface);border-bottom:1px solid var(--border)}
.stat{flex:1;padding:10px 20px;color:var(--muted);border-right:1px solid var(--border);min-width:0}
.stat:last-child{border-right:none}
.stat strong{display:block;font-size:1.25rem;font-weight:700;color:var(--text);line-height:1.2}

.filters{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:10px 20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;
  position:sticky;top:45px;z-index:10;
}
.filters-label{font-size:.75rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-right:2px}
.chip{
  border:1px solid var(--border);background:transparent;color:var(--muted);
  border-radius:20px;padding:3px 11px;font-size:.78rem;cursor:pointer;
  font-family:inherit;transition:background .12s,color .12s,border-color .12s;
}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip.active{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.filters-sep{width:1px;background:var(--border);margin:0 4px;align-self:stretch}

.sort-bar{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:8px 20px;display:flex;gap:6px;align-items:center;
}
.sort-bar .filters-label{margin-right:4px}
.sort-btn{
  border:none;background:none;color:var(--muted);font-size:.78rem;
  cursor:pointer;font-family:inherit;padding:2px 8px;border-radius:4px;
  transition:background .12s,color .12s;
}
.sort-btn:hover{background:var(--border)}
.sort-btn.active{color:var(--accent);font-weight:600}

main{max-width:900px;margin:0 auto;padding:20px 16px}
section+section{margin-top:36px}
h2{font-size:.75rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px}
.empty{color:var(--muted);font-size:.9rem;padding:8px 0}
#no-results{display:none;color:var(--muted);font-size:.9rem;padding:12px 0}

.date-group{margin-bottom:24px}
.date-label{font-size:.75rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-left:2px}

.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:8px;
  box-shadow:var(--shadow);overflow:hidden;display:flex;
  transition:box-shadow .15s;
}
.card:hover{box-shadow:0 2px 14px rgba(0,0,0,.1)}
.card[data-hidden]{display:none!important}
.card-thumb{width:80px;min-width:80px;background:var(--bg);display:flex;align-items:center;justify-content:center}
.card-thumb img{width:80px;height:80px;object-fit:cover;display:block}
.card-no-thumb{width:80px;height:80px;display:flex;align-items:center;justify-content:center;font-size:1.6rem;opacity:.3}
.card-body{padding:12px 14px;flex:1;min-width:0}
.card-title-text{
  font-weight:600;font-size:.9rem;line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}
.card-title-text:hover{text-decoration:underline;text-decoration-color:var(--accent)}
.card-price{font-size:1.05rem;font-weight:700;margin:4px 0 6px;color:var(--text)}
.card-price .est{font-size:.73rem;font-weight:400;color:var(--muted)}
.card-meta{font-size:.77rem;color:var(--muted);display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.cat-tag{
  display:inline-block;background:var(--tag-bg);color:var(--tag-text);
  border-radius:4px;padding:1px 7px;font-size:.71rem;font-weight:600;
}
.tag-maybe{display:inline-block;background:#ede9fe;color:var(--maybe);border-radius:4px;padding:1px 7px;font-size:.71rem;font-weight:600}
@media(prefers-color-scheme:dark){
  :root:not([data-theme=light]) .tag-maybe{background:#1e1b4b}
}

footer{text-align:center;padding:40px 16px 32px;font-size:.78rem;color:var(--muted)}
@media(max-width:500px){
  .stat{padding:10px 14px}.stat strong{font-size:1.05rem}
  .card-thumb,.card-thumb img,.card-no-thumb{width:64px;min-width:64px;height:64px}
  .filters,.sort-bar{padding:8px 12px}
}
"""

_JS = r"""
const cards = () => [...document.querySelectorAll('.card[data-cat]')];
const groups = () => [...document.querySelectorAll('.date-group')];
let activeCats = new Set(), activePeriod = 'all', activeSort = 'newest';

function applyFilters() {
  const now = Date.now();
  const periodMs = {all: Infinity, today: 86400e3, week: 7*86400e3}[activePeriod];
  let visible = 0;
  cards().forEach(c => {
    const catOk = activeCats.size === 0 || activeCats.has(c.dataset.cat);
    const ts = parseInt(c.dataset.ts, 10) * 1000;
    const periodOk = (now - ts) <= periodMs;
    const show = catOk && periodOk;
    show ? c.removeAttribute('data-hidden') : c.setAttribute('data-hidden','1');
    if (show) visible++;
  });
  groups().forEach(g => {
    const any = [...g.querySelectorAll('.card')].some(c => !c.hasAttribute('data-hidden'));
    g.style.display = any ? '' : 'none';
  });
  document.getElementById('no-results').style.display = visible === 0 ? '' : 'none';
  applySort();
}

function applySort() {
  const container = document.getElementById('cards-container');
  if (!container) return;
  const all = [...container.querySelectorAll('.card[data-cat]')].filter(c => !c.hasAttribute('data-hidden'));
  const sorted = all.sort((a, b) => {
    if (activeSort === 'newest') return parseInt(b.dataset.ts) - parseInt(a.dataset.ts);
    if (activeSort === 'oldest') return parseInt(a.dataset.ts) - parseInt(b.dataset.ts);
    if (activeSort === 'price_asc') return (parseInt(a.dataset.price)||0) - (parseInt(b.dataset.price)||0);
    if (activeSort === 'price_desc') return (parseInt(b.dataset.price)||0) - (parseInt(a.dataset.price)||0);
    return 0;
  });
  // Flad liste — fjern date-groups og indsæt direkte i container
  if (activeSort !== 'newest') {
    groups().forEach(g => g.style.display = 'none');
    const flat = document.getElementById('flat-list');
    flat.innerHTML = '';
    sorted.forEach(c => flat.appendChild(c.cloneNode(true)));
    flat.style.display = '';
  } else {
    document.getElementById('flat-list').style.display = 'none';
    document.getElementById('flat-list').innerHTML = '';
    groups().forEach(g => {
      const any = [...g.querySelectorAll('.card')].some(c => !c.hasAttribute('data-hidden'));
      g.style.display = any ? '' : 'none';
    });
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.chip[data-cat]').forEach(btn => {
    btn.addEventListener('click', () => {
      const cat = btn.dataset.cat;
      if (activeCats.has(cat)) { activeCats.delete(cat); btn.classList.remove('active'); }
      else { activeCats.add(cat); btn.classList.add('active'); }
      applyFilters();
    });
  });
  document.querySelectorAll('.chip[data-period]').forEach(btn => {
    btn.addEventListener('click', () => {
      activePeriod = btn.dataset.period;
      document.querySelectorAll('.chip[data-period]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applyFilters();
    });
  });
  document.querySelectorAll('.sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      activeSort = btn.dataset.sort;
      document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applySort();
    });
  });
});
"""


def _build_page(
    last_run: str,
    total_lots: int,
    total_sent: int,
    maybe_count: int,
    notifications_html: str,
    review_section: str,
    category_chips: str,
) -> str:
    maybe_stat = (
        f"<div class='stat'><strong>{maybe_count}</strong>til gennemsyn</div>"
        if maybe_count else ""
    )
    return (
        "<!doctype html><html lang='da'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Auktionshuset Hunter</title>"
        f"<style>{_CSS}</style>"
        "</head><body>"
        "<header>"
        "<h1>🏷 Auktionshuset Hunter</h1>"
        f"<span class='sub'>{last_run}</span>"
        "</header>"
        "<div class='stats-bar'>"
        f"<div class='stat'><strong>{total_lots}</strong>lots set</div>"
        f"<div class='stat'><strong>{total_sent}</strong>fund sendt</div>"
        f"{maybe_stat}"
        "</div>"
        "<div class='filters'>"
        f"<span class='filters-label'>Kategori</span>{category_chips}"
        "<div class='filters-sep'></div>"
        "<span class='filters-label'>Periode</span>"
        "<button class='chip active' data-period='all'>Alle</button>"
        "<button class='chip' data-period='today'>I dag</button>"
        "<button class='chip' data-period='week'>7 dage</button>"
        "</div>"
        "<div class='sort-bar'>"
        "<span class='filters-label'>Sorter</span>"
        "<button class='sort-btn active' data-sort='newest'>Nyeste</button>"
        "<button class='sort-btn' data-sort='oldest'>Ældste</button>"
        "<button class='sort-btn' data-sort='price_desc'>Pris ↓</button>"
        "<button class='sort-btn' data-sort='price_asc'>Pris ↑</button>"
        "</div>"
        "<main>"
        "<section>"
        "<h2>Seneste fund</h2>"
        f"<div id='no-results'>Ingen fund matcher filteret.</div>"
        f"<div id='cards-container'>{notifications_html}</div>"
        "<div id='flat-list' style='display:none'></div>"
        "</section>"
        f"{review_section}"
        "</main>"
        "<footer>Opdateres ved reload &middot; Kun ny data sendes til Discord</footer>"
        f"<script>{_JS}</script>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _recent_notifications(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT n.lot_id, n.category_key, n.sent_at, n.cost,
               l.title, l.url, l.auction_title
        FROM notifications n
        JOIN lots l ON n.lot_id = l.lot_id
        ORDER BY n.sent_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _pending_reviews(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
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


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    result = {}
    for table in ("lots", "notifications"):
        result[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    result["pending_review"] = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE digested_at IS NULL"
    ).fetchone()[0]
    return result


def _last_run_label(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT finished_at FROM runs WHERE finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return "Ingen kørsel registreret"
    try:
        dt = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
        minutes = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
        if minutes < 2:
            return "Sidst kørt: lige nu"
        if minutes < 60:
            return f"Sidst kørt: {minutes} min siden"
        return f"Sidst kørt: {minutes // 60} t siden"
    except ValueError:
        return f"Sidst kørt: {row[0]}"


# ---------------------------------------------------------------------------
# HTML-rendering
# ---------------------------------------------------------------------------

def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
    )


def _ts(iso: str) -> int:
    """ISO-streng til Unix-timestamp (sekunder) til brug i data-ts."""
    try:
        return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return 0


def _rel_time(iso: str) -> tuple[str, str]:
    try:
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        total = int((datetime.now(timezone.utc) - dt).total_seconds())
        if total < 120:
            rel = "lige nu"
        elif total < 3600:
            rel = f"{total // 60} min siden"
        elif total < 86400:
            rel = f"{total // 3600} t siden"
        else:
            rel = f"{total // 86400} dage siden"
        return rel, dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return iso, iso


def _date_label(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).date()
        today = datetime.now(timezone.utc).date()
        if d == today:
            return "I dag"
        if (today - d).days == 1:
            return "I går"
        return d.strftime("%-d. %B")
    except (ValueError, TypeError):
        return iso[:10]


def _render_notifications(rows: list[sqlite3.Row]) -> tuple[str, list[str]]:
    """Returnér (HTML, liste af unikke kategorier) til at bygge filter-chips."""
    if not rows:
        return "<p class='empty'>Ingen fund endnu.</p>", []

    categories: list[str] = []
    seen_cats: set[str] = set()

    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(_date_label(row["sent_at"]), []).append(row)
        cat = row["category_key"] or ""
        if cat and cat not in seen_cats:
            seen_cats.add(cat)
            categories.append(cat)

    parts: list[str] = []
    for label, items in groups.items():
        cards: list[str] = []
        for row in items:
            title = _escape(row["title"] or f"Lot {row['lot_id']}")
            url = row["url"] or ""
            cost = row["cost"] or 0
            price_str = f"{cost:,} kr".replace(",", ".") if cost else "–"
            est_note = ""
            rel, full = _rel_time(row["sent_at"])
            auction = _escape(row["auction_title"] or "")
            cat = _escape(row["category_key"] or "")
            ts = _ts(row["sent_at"])

            thumb = "<div class='card-thumb'><div class='card-no-thumb'>📦</div></div>"

            title_el = (
                f"<a href='{_escape(url)}' target='_blank' rel='noopener'>"
                f"<span class='card-title-text'>{title}</span></a>"
                if url else f"<span class='card-title-text'>{title}</span>"
            )

            cards.append(
                f"<div class='card' data-cat='{cat}' data-ts='{ts}' data-price='{cost}'>"
                f"{thumb}"
                f"<div class='card-body'>"
                f"{title_el}"
                f"<div class='card-price'>{price_str}{est_note}</div>"
                f"<div class='card-meta'>"
                f"<span class='cat-tag'>{cat}</span>"
                f"<span title='{full}'>{rel}</span>"
                f"<span>{auction}</span>"
                f"</div></div></div>"
            )

        parts.append(
            f"<div class='date-group'>"
            f"<div class='date-label'>{_escape(label)}</div>"
            + "".join(cards)
            + "</div>"
        )

    return "".join(parts), categories


def _render_review_section(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return ""

    cards: list[str] = []
    for row in rows:
        title = _escape(row["title"] or "Ukendt titel")
        url = row["url"] or ""
        title_el = (
            f"<a href='{_escape(url)}' target='_blank' rel='noopener'>"
            f"<span class='card-title-text'>{title}</span></a>"
            if url else f"<span class='card-title-text'>{title}</span>"
        )
        reason = _escape(row["reason"] or "")
        cards.append(
            f"<div class='card'>"
            f"<div class='card-body'>"
            f"<div style='margin-bottom:4px'><span class='tag-maybe'>måske</span></div>"
            f"{title_el}"
            f"<div class='card-meta' style='margin-top:6px'>{reason}</div>"
            f"</div></div>"
        )

    return (
        f"<section><h2>Til gennemsyn ({len(rows)})</h2>"
        + "".join(cards)
        + "</section>"
    )


def _render_page(db_path: str) -> bytes:
    try:
        conn = _conn(db_path)
    except Exception:
        return b"<h1>Databasen ikke klar endnu</h1>"

    try:
        notifications = _recent_notifications(conn)
        reviews = _pending_reviews(conn)
        counts = _counts(conn)
        last_run = _last_run_label(conn)

        notifications_html, categories = _render_notifications(notifications)

        chips = "".join(
            f"<button class='chip' data-cat='{_escape(c)}'>{_escape(c)}</button>"
            for c in categories
        )

        html = _build_page(
            last_run=_escape(last_run),
            total_lots=counts["lots"],
            total_sent=counts["notifications"],
            maybe_count=counts["pending_review"],
            notifications_html=notifications_html,
            review_section=_render_review_section(reviews),
            category_chips=chips,
        )
        return html.encode()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP-server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    db_path: str = "data/auction_hunter.db"

    def log_message(self, fmt: str, *args: object) -> None:  # type: ignore[override]
        pass

    def do_GET(self) -> None:
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return

        body = _render_page(self.db_path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    db_path: str = "data/auction_hunter.db",
) -> None:
    _Handler.db_path = db_path

    import logging
    logging.getLogger(__name__).info("Dashboard korer paa http://%s:%d", host, port)

    server = HTTPServer((host, port), _Handler)
    server.serve_forever()
