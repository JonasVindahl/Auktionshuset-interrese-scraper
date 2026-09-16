"""Simpelt webdashboard over sendte fund, feedback og udløbne lots.

Kører som en separat proces og deler SQLite-fil med scraperen (WAL-mode).

    python -m auction_hunter web [--port 8080] [--host 0.0.0.0]

Ruter:
    GET  /          Seneste fund med filtrering, sortering, prisfilter
    GET  /expired   Lots der sluttede inden for de seneste 48 timer
    POST /feedback  Gem manuel markering (skip/bid/bought) som JSON
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
:root{
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
  padding:12px 20px;display:flex;align-items:center;gap:12px;
  position:sticky;top:0;z-index:20;
}
header h1{font-size:.95rem;font-weight:700;letter-spacing:-.01em}
.nav-link{font-size:.82rem;color:var(--muted);padding:3px 10px;border-radius:6px;border:1px solid transparent}
.nav-link:hover,.nav-link.active{border-color:var(--border);color:var(--text)}
header .sub{color:var(--muted);font-size:.8rem;margin-left:auto}

.stats-bar{display:flex;background:var(--surface);border-bottom:1px solid var(--border)}
.stat{flex:1;padding:10px 20px;color:var(--muted);border-right:1px solid var(--border);min-width:0}
.stat:last-child{border-right:none}
.stat strong{display:block;font-size:1.25rem;font-weight:700;color:var(--text);line-height:1.2}

.filter-bar{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:8px 20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;
  position:sticky;top:45px;z-index:10;
}
.filter-label{font-size:.72rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;white-space:nowrap}
.filter-sep{width:1px;background:var(--border);margin:0 2px;align-self:stretch;min-height:20px}
.chip{
  border:1px solid var(--border);background:transparent;color:var(--muted);
  border-radius:20px;padding:3px 11px;font-size:.77rem;cursor:pointer;
  font-family:inherit;transition:background .12s,color .12s,border-color .12s;white-space:nowrap;
}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip.active{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.price-inputs{display:flex;gap:4px;align-items:center}
.price-inputs input{
  width:72px;padding:3px 7px;font-size:.77rem;font-family:inherit;
  border:1px solid var(--border);border-radius:20px;background:transparent;color:var(--text);
  outline:none;
}
.price-inputs input:focus{border-color:var(--accent)}
.price-inputs span{font-size:.77rem;color:var(--muted)}

.sort-bar{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:7px 20px;display:flex;gap:4px;align-items:center;flex-wrap:wrap;
}
.sort-btn{
  border:none;background:none;color:var(--muted);font-size:.77rem;
  cursor:pointer;font-family:inherit;padding:2px 9px;border-radius:4px;
  transition:background .12s,color .12s;
}
.sort-btn:hover{background:var(--border)}
.sort-btn.active{color:var(--accent);font-weight:700}

main{max-width:900px;margin:0 auto;padding:20px 16px}
section+section{margin-top:36px}
h2{font-size:.73rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px}
.empty{color:var(--muted);font-size:.9rem;padding:8px 0}
#no-results{display:none;color:var(--muted);padding:12px 0}

.date-group{margin-bottom:24px}
.date-label{font-size:.73rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px;padding-left:2px}

.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:8px;
  box-shadow:var(--shadow);overflow:hidden;display:flex;
  transition:box-shadow .15s,opacity .2s;
}
.card:hover{box-shadow:0 2px 14px rgba(0,0,0,.1)}
.card[data-hidden]{display:none!important}
.card[data-feedback=skip]{opacity:.45}
.card[data-feedback=bought]{border-left:3px solid var(--accent)}
.card[data-feedback=bid]{border-left:3px solid var(--over)}

.card-thumb{width:80px;min-width:80px;background:var(--bg);display:flex;align-items:center;justify-content:center}
.card-no-thumb{width:80px;height:80px;display:flex;align-items:center;justify-content:center;font-size:1.5rem;opacity:.28}
.card-body{padding:12px 14px;flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
.card-title-link{display:block}
.card-title-text{
  font-weight:600;font-size:.9rem;line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}
.card-title-text:hover{text-decoration:underline;text-decoration-color:var(--accent)}
.card-price{font-size:1.05rem;font-weight:700;color:var(--text)}
.card-meta{font-size:.76rem;color:var(--muted);display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.cat-tag{
  display:inline-block;background:var(--tag-bg);color:var(--tag-text);
  border-radius:4px;padding:1px 7px;font-size:.7rem;font-weight:700;
}
.tag-maybe{display:inline-block;background:#ede9fe;color:var(--maybe);border-radius:4px;padding:1px 7px;font-size:.7rem;font-weight:700}
@media(prefers-color-scheme:dark){
  :root:not([data-theme=light]) .tag-maybe{background:#1e1b4b}
}
.ended-badge{color:var(--muted);font-size:.72rem}
.ended-badge.soon{color:var(--over);font-weight:600}

.feedback-btns{display:flex;gap:4px;margin-top:2px}
.fb-btn{
  border:1px solid var(--border);background:transparent;color:var(--muted);
  border-radius:6px;padding:2px 9px;font-size:.72rem;cursor:pointer;
  font-family:inherit;transition:background .1s,color .1s,border-color .1s;
}
.fb-btn:hover{border-color:currentColor}
.fb-btn[data-action=skip]:hover{color:#e74c3c;border-color:#e74c3c}
.fb-btn[data-action=bid]:hover{color:var(--over);border-color:var(--over)}
.fb-btn[data-action=bought]:hover{color:var(--accent);border-color:var(--accent)}
.fb-btn.active[data-action=skip]{color:#e74c3c;border-color:#e74c3c;background:#fef2f2}
.fb-btn.active[data-action=bid]{color:var(--over);border-color:var(--over);background:#fffbeb}
.fb-btn.active[data-action=bought]{color:var(--accent);border-color:var(--accent);background:var(--tag-bg)}
@media(prefers-color-scheme:dark){
  :root:not([data-theme=light]) .fb-btn.active[data-action=skip]{background:#3b0a0a}
  :root:not([data-theme=light]) .fb-btn.active[data-action=bid]{background:#3a1e00}
  :root:not([data-theme=light]) .fb-btn.active[data-action=bought]{background:var(--tag-bg)}
}

footer{text-align:center;padding:40px 16px 32px;font-size:.78rem;color:var(--muted)}
@media(max-width:500px){
  .stat{padding:10px 14px}.stat strong{font-size:1.05rem}
  .card-no-thumb,.card-thumb{width:64px;min-width:64px;height:64px}
  .filter-bar,.sort-bar{padding:8px 12px}
}
"""

# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

_JS = r"""
const allCards = () => [...document.querySelectorAll('.card[data-cat]')];
const allGroups = () => [...document.querySelectorAll('.date-group')];
let activeCats = new Set(), activePeriod = 'all', activeSort = 'newest';
let priceMin = 0, priceMax = Infinity;

function applyFilters() {
  const now = Date.now();
  const periodMs = {all: Infinity, today: 86400e3, week: 7*86400e3}[activePeriod] ?? Infinity;
  let visible = 0;
  allCards().forEach(c => {
    const catOk = activeCats.size === 0 || activeCats.has(c.dataset.cat);
    const tsOk = (now - parseInt(c.dataset.ts,10)*1000) <= periodMs;
    const price = parseInt(c.dataset.price,10) || 0;
    const priceOk = price >= priceMin && price <= priceMax;
    const show = catOk && tsOk && priceOk;
    show ? c.removeAttribute('data-hidden') : c.setAttribute('data-hidden','1');
    if (show) visible++;
  });
  const noRes = document.getElementById('no-results');
  if (noRes) noRes.style.display = visible === 0 ? '' : 'none';
  applySort();
}

function applySort() {
  const flat = document.getElementById('flat-list');
  const visible = allCards().filter(c => !c.hasAttribute('data-hidden'));
  if (activeSort === 'newest') {
    if (flat) { flat.style.display='none'; flat.innerHTML=''; }
    allGroups().forEach(g => {
      const any = [...g.querySelectorAll('.card')].some(c => !c.hasAttribute('data-hidden'));
      g.style.display = any ? '' : 'none';
    });
  } else {
    allGroups().forEach(g => g.style.display='none');
    if (!flat) return;
    const sorted = visible.sort((a,b) => {
      if (activeSort==='oldest') return parseInt(a.dataset.ts)-parseInt(b.dataset.ts);
      const pa = parseInt(a.dataset.price)||0, pb = parseInt(b.dataset.price)||0;
      return activeSort==='price_asc' ? pa-pb : pb-pa;
    });
    flat.innerHTML = '';
    sorted.forEach(c => flat.appendChild(c.cloneNode(true)));
    // Genbind feedback-knapper i flat list
    flat.querySelectorAll('.fb-btn').forEach(bindFeedbackBtn);
    flat.style.display = '';
  }
}

function bindFeedbackBtn(btn) {
  btn.addEventListener('click', async e => {
    e.stopPropagation();
    const card = btn.closest('.card');
    const {lotId, catKey, action} = btn.dataset;
    const title = card?.querySelector('.card-title-text')?.textContent?.trim() || '';
    const current = card?.dataset.feedback;
    const newAction = current === action ? '' : action;
    try {
      await fetch('/feedback', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({lot_id: lotId, category_key: catKey, action: newAction, title})
      });
    } catch(_) {}
    // Opdater alle kort med samme lot_id
    document.querySelectorAll(`.card[data-lot="${lotId}"]`).forEach(c => {
      c.dataset.feedback = newAction;
      c.querySelectorAll('.fb-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.action === newAction && newAction !== '');
      });
    });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  // Kategori-chips
  document.querySelectorAll('.chip[data-cat]').forEach(btn => {
    btn.addEventListener('click', () => {
      const cat = btn.dataset.cat;
      activeCats.has(cat) ? (activeCats.delete(cat), btn.classList.remove('active'))
                           : (activeCats.add(cat), btn.classList.add('active'));
      applyFilters();
    });
  });
  // Periode-chips
  document.querySelectorAll('.chip[data-period]').forEach(btn => {
    btn.addEventListener('click', () => {
      activePeriod = btn.dataset.period;
      document.querySelectorAll('.chip[data-period]').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      applyFilters();
    });
  });
  // Sortering
  document.querySelectorAll('.sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      activeSort = btn.dataset.sort;
      document.querySelectorAll('.sort-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      applySort();
    });
  });
  // Prisfilter
  const pMin = document.getElementById('price-min');
  const pMax = document.getElementById('price-max');
  if (pMin) pMin.addEventListener('input', () => { priceMin = parseInt(pMin.value)||0; applyFilters(); });
  if (pMax) pMax.addEventListener('input', () => { priceMax = parseInt(pMax.value)||Infinity; applyFilters(); });
  // Feedback-knapper
  document.querySelectorAll('.fb-btn').forEach(bindFeedbackBtn);
});
"""

# ---------------------------------------------------------------------------
# Hjælpefunktioner
# ---------------------------------------------------------------------------

def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
    )


def _ts(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return 0


def _rel_time(iso: str) -> tuple[str, str]:
    try:
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        total = int((datetime.now(timezone.utc) - dt).total_seconds())
        if total < 0:
            secs = abs(total)
            if secs < 3600:
                return f"om {secs//60} min", dt.strftime("%Y-%m-%d %H:%M UTC")
            return f"om {secs//3600} t", dt.strftime("%Y-%m-%d %H:%M UTC")
        if total < 120:
            rel = "lige nu"
        elif total < 3600:
            rel = f"{total//60} min siden"
        elif total < 86400:
            rel = f"{total//3600} t siden"
        else:
            rel = f"{total//86400} dage siden"
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


def _ro_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rw_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

# ---------------------------------------------------------------------------
# Database-forespørgsler
# ---------------------------------------------------------------------------

def _recent_notifications(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT n.lot_id, n.category_key, n.sent_at, n.cost,
               l.title, l.url, l.auction_title, l.ends_at,
               COALESCE(f.action, '') AS feedback_action
        FROM notifications n
        JOIN lots l ON n.lot_id = l.lot_id
        LEFT JOIN feedback f ON f.lot_id = n.lot_id AND f.category_key = n.category_key
        ORDER BY n.sent_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _expired_lots(conn: sqlite3.Connection, hours: int = 48) -> list[sqlite3.Row]:
    """Lots der sluttede inden for de seneste N timer og var i notifikationer."""
    return conn.execute(
        """
        SELECT n.lot_id, n.category_key, n.sent_at, n.cost,
               l.title, l.url, l.auction_title, l.ends_at,
               COALESCE(f.action, '') AS feedback_action
        FROM notifications n
        JOIN lots l ON n.lot_id = l.lot_id
        LEFT JOIN feedback f ON f.lot_id = n.lot_id AND f.category_key = n.category_key
        WHERE l.ends_at IS NOT NULL
          AND l.ends_at < datetime('now', 'localtime')
          AND l.ends_at > datetime('now', 'localtime', ? || ' hours')
        ORDER BY l.ends_at DESC
        """,
        (f"-{hours}",),
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
        return "Ingen kørsel endnu"
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


def _save_feedback(db_path: str, lot_id: str, category_key: str, action: str, title: str) -> None:
    """Gem eller slet feedback. Tom action sletter rækken."""
    with _rw_conn(db_path) as conn:
        if action:
            conn.execute(
                """INSERT OR REPLACE INTO feedback (lot_id, category_key, action, title, created_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (lot_id, category_key, action, title),
            )
        else:
            conn.execute(
                "DELETE FROM feedback WHERE lot_id=? AND category_key=?",
                (lot_id, category_key),
            )
        conn.commit()

# ---------------------------------------------------------------------------
# HTML-rendering
# ---------------------------------------------------------------------------

def _render_card(row: sqlite3.Row, *, show_ends: bool = False) -> str:
    lot_id = _escape(row["lot_id"] or "")
    cat = _escape(row["category_key"] or "")
    title = _escape(row["title"] or f"Lot {lot_id}")
    url = row["url"] or ""
    cost = row["cost"] or 0
    price_str = f"{cost:,} kr".replace(",", ".") if cost else "–"
    ts = _ts(row["sent_at"])
    auction = _escape(row["auction_title"] or "")
    feedback = _escape(row.get("feedback_action") or "")

    title_el = (
        f"<a class='card-title-link' href='{_escape(url)}' target='_blank' rel='noopener'>"
        f"<span class='card-title-text'>{title}</span></a>"
        if url else f"<span class='card-title-text'>{title}</span>"
    )

    ends_badge = ""
    if show_ends and row["ends_at"]:
        rel, full = _rel_time(row["ends_at"])
        ended_ago = "soon" if "om " in rel else ""
        ends_badge = f"<span class='ended-badge {ended_ago}' title='{full}'>Sluttede {rel}</span>"

    fb_skip = "active" if feedback == "skip" else ""
    fb_bid = "active" if feedback == "bid" else ""
    fb_bought = "active" if feedback == "bought" else ""

    return (
        f"<div class='card' data-cat='{cat}' data-ts='{ts}' data-price='{cost}'"
        f" data-lot='{lot_id}' data-feedback='{feedback}'>"
        f"<div class='card-thumb'><div class='card-no-thumb'>📦</div></div>"
        f"<div class='card-body'>"
        f"{title_el}"
        f"<div class='card-price'>{price_str}</div>"
        f"<div class='card-meta'>"
        f"<span class='cat-tag'>{cat}</span>"
        f"{ends_badge}"
        f"<span>{auction}</span>"
        f"</div>"
        f"<div class='feedback-btns'>"
        f"<button class='fb-btn {fb_skip}' data-action='skip' data-lot-id='{lot_id}' data-cat-key='{cat}'"
        f" title='Ikke interesseret'>✕ Ikke interes.</button>"
        f"<button class='fb-btn {fb_bid}' data-action='bid' data-lot-id='{lot_id}' data-cat-key='{cat}'"
        f" title='Jeg har budt'>📌 Budt</button>"
        f"<button class='fb-btn {fb_bought}' data-action='bought' data-lot-id='{lot_id}' data-cat-key='{cat}'"
        f" title='Jeg har købt det'>✅ Købt</button>"
        f"</div>"
        f"</div></div>"
    )


def _render_notifications(rows: list[sqlite3.Row]) -> tuple[str, list[str]]:
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
        cards = "".join(_render_card(r) for r in items)
        parts.append(
            f"<div class='date-group'>"
            f"<div class='date-label'>{_escape(label)}</div>"
            f"{cards}</div>"
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
            f"<a class='card-title-link' href='{_escape(url)}' target='_blank' rel='noopener'>"
            f"<span class='card-title-text'>{title}</span></a>"
            if url else f"<span class='card-title-text'>{title}</span>"
        )
        reason = _escape(row["reason"] or "")
        cards.append(
            f"<div class='card'><div class='card-body'>"
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

# ---------------------------------------------------------------------------
# Side-builder
# ---------------------------------------------------------------------------

def _header(active: str, last_run: str) -> str:
    main_cls = "active" if active == "main" else ""
    exp_cls = "active" if active == "expired" else ""
    return (
        "<header>"
        "<h1>🏷 Auktionshuset Hunter</h1>"
        f"<a href='/' class='nav-link {main_cls}'>Fund</a>"
        f"<a href='/expired' class='nav-link {exp_cls}'>Udløbet</a>"
        f"<span class='sub'>{last_run}</span>"
        "</header>"
    )


def _filter_bar(category_chips: str) -> str:
    return (
        "<div class='filter-bar'>"
        f"<span class='filter-label'>Kat.</span>{category_chips}"
        "<div class='filter-sep'></div>"
        "<span class='filter-label'>Periode</span>"
        "<button class='chip active' data-period='all'>Alle</button>"
        "<button class='chip' data-period='today'>I dag</button>"
        "<button class='chip' data-period='week'>7 dage</button>"
        "<div class='filter-sep'></div>"
        "<span class='filter-label'>Pris</span>"
        "<div class='price-inputs'>"
        "<input id='price-min' type='number' min='0' placeholder='Fra' inputmode='numeric'>"
        "<span>–</span>"
        "<input id='price-max' type='number' min='0' placeholder='Til' inputmode='numeric'>"
        "<span style='color:var(--muted);font-size:.77rem'>kr</span>"
        "</div>"
        "</div>"
        "<div class='sort-bar'>"
        "<span class='filter-label' style='margin-right:4px'>Sorter</span>"
        "<button class='sort-btn active' data-sort='newest'>Nyeste</button>"
        "<button class='sort-btn' data-sort='oldest'>Ældste</button>"
        "<button class='sort-btn' data-sort='price_desc'>Pris ↓</button>"
        "<button class='sort-btn' data-sort='price_asc'>Pris ↑</button>"
        "</div>"
    )


def _page_shell(*, header: str, stats: str, filters: str, body: str) -> str:
    return (
        "<!doctype html><html lang='da'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Auktionshuset Hunter</title>"
        f"<style>{_CSS}</style>"
        "</head><body>"
        f"{header}{stats}{filters}"
        f"<main>{body}</main>"
        "<footer>Fund sendes til Discord &middot; Feedback gemmes lokalt til AI-træning</footer>"
        f"<script>{_JS}</script>"
        "</body></html>"
    )


def _render_main_page(db_path: str) -> bytes:
    try:
        conn = _ro_conn(db_path)
    except Exception:
        return b"<h1>Databasen ikke klar</h1>"
    try:
        rows = _recent_notifications(conn)
        reviews = _pending_reviews(conn)
        counts = _counts(conn)
        last_run = _last_run_label(conn)
        notifications_html, categories = _render_notifications(rows)
        maybe_count = counts["pending_review"]
        chips = "".join(
            f"<button class='chip' data-cat='{_escape(c)}'>{_escape(c)}</button>"
            for c in categories
        )
        maybe_stat = (
            f"<div class='stat'><strong>{maybe_count}</strong>til gennemsyn</div>"
            if maybe_count else ""
        )
        stats = (
            "<div class='stats-bar'>"
            f"<div class='stat'><strong>{counts['lots']}</strong>lots set</div>"
            f"<div class='stat'><strong>{counts['notifications']}</strong>fund sendt</div>"
            f"{maybe_stat}"
            "</div>"
        )
        body = (
            "<section>"
            "<h2>Seneste fund</h2>"
            "<div id='no-results'>Ingen fund matcher filteret.</div>"
            f"<div id='cards-container'>{notifications_html}</div>"
            "<div id='flat-list' style='display:none'></div>"
            "</section>"
            f"{_render_review_section(reviews)}"
        )
        html = _page_shell(
            header=_header("main", _escape(last_run)),
            stats=stats,
            filters=_filter_bar(chips),
            body=body,
        )
        return html.encode()
    finally:
        conn.close()


def _render_expired_page(db_path: str) -> bytes:
    try:
        conn = _ro_conn(db_path)
    except Exception:
        return b"<h1>Databasen ikke klar</h1>"
    try:
        rows = _expired_lots(conn, hours=48)
        last_run = _last_run_label(conn)
        counts = _counts(conn)

        if not rows:
            body = "<section><h2>Udl&oslash;bne lots (48 t)</h2><p class='empty'>Ingen lots afsluttet inden for de seneste 48 timer.</p></section>"
        else:
            categories: list[str] = []
            seen: set[str] = set()
            for r in rows:
                c = r["category_key"] or ""
                if c and c not in seen:
                    seen.add(c)
                    categories.append(c)

            chips = "".join(
                f"<button class='chip' data-cat='{_escape(c)}'>{_escape(c)}</button>"
                for c in categories
            )
            cards_html = "".join(_render_card(r, show_ends=True) for r in rows)
            body = (
                f"<section>"
                f"<h2>Udl&oslash;bne lots ({len(rows)} seneste 48 t)</h2>"
                "<div id='no-results'>Ingen fund matcher filteret.</div>"
                f"<div id='cards-container'>{cards_html}</div>"
                "<div id='flat-list' style='display:none'></div>"
                "</section>"
            )

        stats = (
            "<div class='stats-bar'>"
            f"<div class='stat'><strong>{counts['lots']}</strong>lots set</div>"
            f"<div class='stat'><strong>{counts['notifications']}</strong>fund sendt</div>"
            "</div>"
        )

        # Byg filter-bar til expired (ingen periode-filter — alle er i 48t-vinduet)
        cat_chips_for_expired = "".join(
            f"<button class='chip' data-cat='{_escape(c)}'>{_escape(c)}</button>"
            for c in (categories if rows else [])
        )
        filters = (
            "<div class='filter-bar'>"
            f"<span class='filter-label'>Kat.</span>{cat_chips_for_expired}"
            "<div class='filter-sep'></div>"
            "<span class='filter-label'>Pris</span>"
            "<div class='price-inputs'>"
            "<input id='price-min' type='number' min='0' placeholder='Fra' inputmode='numeric'>"
            "<span>–</span>"
            "<input id='price-max' type='number' min='0' placeholder='Til' inputmode='numeric'>"
            "<span style='color:var(--muted);font-size:.77rem'>kr</span>"
            "</div>"
            "</div>"
            "<div class='sort-bar'>"
            "<span class='filter-label' style='margin-right:4px'>Sorter</span>"
            "<button class='sort-btn active' data-sort='newest'>Nyeste</button>"
            "<button class='sort-btn' data-sort='price_desc'>Pris ↓</button>"
            "<button class='sort-btn' data-sort='price_asc'>Pris ↑</button>"
            "</div>"
        )

        html = _page_shell(
            header=_header("expired", _escape(last_run)),
            stats=stats,
            filters=filters,
            body=body,
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
        path = self.path.split("?")[0]
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/expired":
            body = _render_expired_page(self.db_path)
        elif path == "/":
            body = _render_main_page(self.db_path)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/feedback":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
            lot_id = str(data.get("lot_id", ""))
            cat = str(data.get("category_key", ""))
            action = str(data.get("action", ""))
            title = str(data.get("title", ""))[:200]
            if not lot_id or action not in ("skip", "bid", "bought", ""):
                raise ValueError("ugyldigt input")
            _save_feedback(self.db_path, lot_id, cat, action, title)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"ok":false}')


def serve(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    db_path: str = "data/auction_hunter.db",
) -> None:
    _Handler.db_path = db_path
    import logging
    logging.getLogger(__name__).info("Dashboard paa http://%s:%d", host, port)
    HTTPServer((host, port), _Handler).serve_forever()
