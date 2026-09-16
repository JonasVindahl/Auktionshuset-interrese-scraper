"""Webdashboard over sendte fund, feedback og udløbne lots.

Kører som en separat proces og deler SQLite-fil med scraperen (WAL-mode).

    python -m auction_hunter web [--port 8080] [--host 0.0.0.0]

Ruter:
    GET  /          Aktive fund: filtrering, sortering, prisfilter, tid tilbage
    GET  /expired   Lots der sluttede inden for de seneste 48 timer
    POST /feedback  Gem manuel markering (skip/bid/bought) som JSON

Tidsfiltrering sker i Python, ikke i SQL. ``ends_at`` gemmes som ISO med
tidszone (``2026-09-16T14:30:00+02:00``), og SQLites ``datetime('now')`` giver
``2026-09-16 12:05:24``. En strengsammenligning mellem de to er forkert, fordi
``T`` sorterer efter mellemrum — så alle lots ville se ud til at ligge i
fremtiden.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

# Hvor langt tilbage /expired kigger.
EXPIRED_WINDOW_HOURS = 48

# Under så mange timer tilbage markeres et lot som "haster".
URGENT_HOURS = 6
SOON_HOURS = 24

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
:root{
  --bg:#f4f4f2;--surface:#fff;--border:#e4e4de;--text:#191917;
  --muted:#6b6b65;--faint:#9a9a92;--accent:#2a7d4f;--urgent:#dc2626;
  --warn:#b45309;--maybe:#4f46e5;--rise:#dc2626;
  --tag-bg:#eef2eb;--tag-text:#2a7d4f;--radius:12px;
  --shadow:0 1px 2px rgba(0,0,0,.05),0 2px 10px rgba(0,0,0,.04);
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#101010;--surface:#1b1b19;--border:#2c2c28;--text:#e9e9e3;
    --muted:#8a8a82;--faint:#61615a;--accent:#4ade80;--urgent:#f87171;
    --warn:#fbbf24;--maybe:#818cf8;--rise:#f87171;
    --tag-bg:#1a2e20;--tag-text:#4ade80;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 2px 10px rgba(0,0,0,.25);
  }
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}

header{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:11px 20px;display:flex;align-items:center;gap:6px;
  position:sticky;top:0;z-index:30;
}
header h1{font-size:.92rem;font-weight:700;letter-spacing:-.015em;margin-right:8px}
.nav-link{
  font-size:.82rem;color:var(--muted);padding:4px 11px;border-radius:7px;
  transition:background .12s,color .12s;
}
.nav-link:hover{background:var(--bg);color:var(--text)}
.nav-link.active{background:var(--tag-bg);color:var(--tag-text);font-weight:600}
.pulse{
  margin-left:auto;display:flex;align-items:center;gap:6px;
  color:var(--muted);font-size:.78rem;
}
.dot{width:6px;height:6px;border-radius:50%;background:var(--accent);flex:none}
.dot.stale{background:var(--warn)}

.stats-bar{display:flex;background:var(--surface);border-bottom:1px solid var(--border)}
.stat{
  flex:1;padding:11px 20px;color:var(--muted);font-size:.76rem;
  border-right:1px solid var(--border);min-width:0;
}
.stat:last-child{border-right:none}
.stat strong{display:block;font-size:1.3rem;font-weight:700;color:var(--text);line-height:1.15;letter-spacing:-.02em}
.stat.accent strong{color:var(--accent)}

.controls{background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:44px;z-index:20}
.filter-row{padding:8px 20px;display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.filter-row+.filter-row{border-top:1px solid var(--border)}
.filter-label{
  font-size:.68rem;font-weight:700;color:var(--faint);
  text-transform:uppercase;letter-spacing:.09em;white-space:nowrap;
}
.filter-sep{width:1px;background:var(--border);align-self:stretch;min-height:18px;margin:0 3px}
.chip{
  border:1px solid var(--border);background:transparent;color:var(--muted);
  border-radius:20px;padding:3px 12px;font-size:.77rem;cursor:pointer;
  font-family:inherit;transition:all .12s;white-space:nowrap;
}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip.active{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.chip.urgent.active{background:var(--urgent);border-color:var(--urgent)}
.price-inputs{display:flex;gap:4px;align-items:center}
.price-inputs input{
  width:76px;padding:3px 10px;font-size:.77rem;font-family:inherit;
  border:1px solid var(--border);border-radius:20px;background:transparent;
  color:var(--text);outline:none;transition:border-color .12s;
}
.price-inputs input:focus{border-color:var(--accent)}
.price-inputs input::-webkit-outer-spin-button,
.price-inputs input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.price-inputs input[type=number]{-moz-appearance:textfield}
.price-inputs span{font-size:.77rem;color:var(--faint)}
.sort-btn{
  border:none;background:none;color:var(--muted);font-size:.77rem;
  cursor:pointer;font-family:inherit;padding:3px 10px;border-radius:6px;
  transition:all .12s;
}
.sort-btn:hover{background:var(--bg)}
.sort-btn.active{color:var(--accent);font-weight:700;background:var(--tag-bg)}
.reset-btn{
  margin-left:auto;border:none;background:none;color:var(--faint);
  font-size:.75rem;cursor:pointer;font-family:inherit;padding:3px 8px;
  border-radius:6px;transition:color .12s;
}
.reset-btn:hover{color:var(--urgent)}

main{max-width:940px;margin:0 auto;padding:20px 16px 0}
section+section{margin-top:34px}
h2{
  font-size:.7rem;font-weight:700;color:var(--faint);text-transform:uppercase;
  letter-spacing:.1em;margin-bottom:13px;display:flex;align-items:center;gap:8px;
}
h2 .count{background:var(--border);color:var(--muted);border-radius:10px;padding:1px 8px;font-size:.7rem;letter-spacing:0}
.empty{color:var(--muted);font-size:.9rem;padding:20px 0;text-align:center}
#no-results{display:none;color:var(--muted);padding:28px 0;text-align:center;font-size:.9rem}
#visible-count{font-size:.73rem;color:var(--faint);padding:0 2px 10px}

.date-group{margin-bottom:22px}
.date-label{
  font-size:.7rem;font-weight:700;color:var(--faint);text-transform:uppercase;
  letter-spacing:.09em;margin-bottom:8px;padding-left:2px;
}

.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:9px;box-shadow:var(--shadow);
  overflow:hidden;display:flex;transition:box-shadow .15s,opacity .2s,transform .1s;
}
.card:hover{box-shadow:0 2px 16px rgba(0,0,0,.09)}
.card[data-hidden]{display:none!important}
.card[data-feedback=skip]{opacity:.42}
.card[data-feedback=bought]{box-shadow:inset 3px 0 0 var(--accent),var(--shadow)}
.card[data-feedback=bid]{box-shadow:inset 3px 0 0 var(--warn),var(--shadow)}

.thumb{width:104px;min-width:104px;background:var(--bg);position:relative;overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb-empty{
  width:100%;height:100%;min-height:104px;display:flex;align-items:center;
  justify-content:center;font-size:1.5rem;opacity:.22;
}
.body{padding:12px 15px;flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}
.title{
  font-weight:600;font-size:.91rem;line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}
a:hover .title{text-decoration:underline;text-decoration-color:var(--accent);text-underline-offset:2px}

.price-row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.price{font-size:1.1rem;font-weight:700;letter-spacing:-.02em}
.price-rise{font-size:.73rem;color:var(--rise);font-weight:600}
.price-note{font-size:.72rem;color:var(--faint)}

.meta{font-size:.76rem;color:var(--muted);display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.meta .sep{color:var(--border)}
.cat-tag{
  display:inline-block;background:var(--tag-bg);color:var(--tag-text);
  border-radius:5px;padding:1px 8px;font-size:.7rem;font-weight:700;
  letter-spacing:.01em;
}
.time-left{font-weight:600;font-size:.75rem}
.time-left.urgent{color:var(--urgent)}
.time-left.soon{color:var(--warn)}
.time-left.ended{color:var(--faint);font-weight:400}
.auction-name{
  color:var(--faint);font-size:.74rem;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;max-width:100%;
}
.tag-maybe{display:inline-block;background:#ede9fe;color:var(--maybe);border-radius:5px;padding:1px 8px;font-size:.7rem;font-weight:700}
@media(prefers-color-scheme:dark){
  :root:not([data-theme=light]) .tag-maybe{background:#1e1b4b}
}

.fb-row{display:flex;gap:5px;margin-top:3px;flex-wrap:wrap}
.fb-btn{
  border:1px solid var(--border);background:transparent;color:var(--muted);
  border-radius:7px;padding:3px 10px;font-size:.72rem;cursor:pointer;
  font-family:inherit;transition:all .12s;white-space:nowrap;
}
.fb-btn:hover{border-color:currentColor}
.fb-btn[data-action=skip]:hover{color:var(--urgent);border-color:var(--urgent)}
.fb-btn[data-action=bid]:hover{color:var(--warn);border-color:var(--warn)}
.fb-btn[data-action=bought]:hover{color:var(--accent);border-color:var(--accent)}
.fb-btn.active[data-action=skip]{color:var(--urgent);border-color:var(--urgent);background:rgba(220,38,38,.08);font-weight:600}
.fb-btn.active[data-action=bid]{color:var(--warn);border-color:var(--warn);background:rgba(180,83,9,.08);font-weight:600}
.fb-btn.active[data-action=bought]{color:var(--accent);border-color:var(--accent);background:var(--tag-bg);font-weight:600}

footer{text-align:center;padding:44px 16px 34px;font-size:.76rem;color:var(--faint)}
@media(max-width:560px){
  header{padding:10px 14px}
  .stat{padding:10px 14px}.stat strong{font-size:1.1rem}
  .filter-row{padding:8px 14px}
  main{padding:16px 12px 0}
  .thumb{width:80px;min-width:80px}
  .thumb-empty{min-height:80px}
  .body{padding:10px 12px}
  .reset-btn{margin-left:0}
}
"""

# ---------------------------------------------------------------------------
# JavaScript
#
# Kortene i #cards-container er sandheden. Ved anden sortering end 'nyeste'
# klones de synlige kort ind i #flat-list; derfor scopes alle opslag til
# containeren, ellers ville klonerne tælle med og listen vokse ved hvert klik.
# ---------------------------------------------------------------------------

_JS = r"""
const container = () => document.getElementById('cards-container');
const srcCards = () => container() ? [...container().querySelectorAll('.card[data-cat]')] : [];
const srcGroups = () => container() ? [...container().querySelectorAll('.date-group')] : [];

let activeCats = new Set(), activePeriod = 'all', activeSort = 'newest';
let priceMin = 0, priceMax = Infinity, endingOnly = false;

function applyFilters() {
  const now = Date.now();
  const periodMs = {all: Infinity, today: 86400e3, week: 7*86400e3}[activePeriod] ?? Infinity;
  let visible = 0;
  srcCards().forEach(c => {
    const catOk = activeCats.size === 0 || activeCats.has(c.dataset.cat);
    const tsOk = (now - parseInt(c.dataset.ts, 10) * 1000) <= periodMs;
    const price = parseInt(c.dataset.price, 10) || 0;
    const priceOk = price >= priceMin && price <= priceMax;
    const endsIn = parseInt(c.dataset.endsin, 10);
    const endingOk = !endingOnly || (endsIn > 0 && endsIn <= 86400);
    const show = catOk && tsOk && priceOk && endingOk;
    show ? c.removeAttribute('data-hidden') : c.setAttribute('data-hidden', '1');
    if (show) visible++;
  });
  const noRes = document.getElementById('no-results');
  if (noRes) noRes.style.display = visible === 0 ? '' : 'none';
  const vc = document.getElementById('visible-count');
  const total = srcCards().length;
  if (vc) vc.textContent = visible === total ? `${total} fund` : `${visible} af ${total} fund`;
  applySort();
}

function applySort() {
  const flat = document.getElementById('flat-list');
  if (activeSort === 'newest') {
    if (flat) { flat.style.display = 'none'; flat.innerHTML = ''; }
    srcGroups().forEach(g => {
      const any = [...g.querySelectorAll('.card')].some(c => !c.hasAttribute('data-hidden'));
      g.style.display = any ? '' : 'none';
    });
    return;
  }
  srcGroups().forEach(g => g.style.display = 'none');
  if (!flat) return;
  const visible = srcCards().filter(c => !c.hasAttribute('data-hidden'));
  const num = (c, key) => parseInt(c.dataset[key], 10) || 0;
  const sorted = visible.slice().sort((a, b) => {
    switch (activeSort) {
      case 'oldest':     return num(a,'ts') - num(b,'ts');
      case 'price_asc':  return num(a,'price') - num(b,'price');
      case 'price_desc': return num(b,'price') - num(a,'price');
      case 'ending': {
        // Lots uden sluttidspunkt sidst; afsluttede efter de aktive.
        const ea = num(a,'endsin'), eb = num(b,'endsin');
        const ka = ea > 0 ? ea : Infinity, kb = eb > 0 ? eb : Infinity;
        return ka - kb;
      }
      default: return 0;
    }
  });
  flat.innerHTML = '';
  sorted.forEach(c => flat.appendChild(c.cloneNode(true)));
  flat.querySelectorAll('.fb-btn').forEach(bindFeedback);
  flat.style.display = '';
}

async function bindFeedbackClick(btn) {
  const card = btn.closest('.card');
  const {lotId, catKey, action} = btn.dataset;
  const title = card ? (card.querySelector('.title')?.textContent || '').trim() : '';
  const next = card && card.dataset.feedback === action ? '' : action;
  // Opdatér med det samme; serveren er hurtigere end brugeren kan nå at se.
  document.querySelectorAll(`.card[data-lot="${CSS.escape(lotId)}"]`).forEach(c => {
    c.dataset.feedback = next;
    c.querySelectorAll('.fb-btn').forEach(b => {
      b.classList.toggle('active', next !== '' && b.dataset.action === next);
    });
  });
  try {
    await fetch('/feedback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lot_id: lotId, category_key: catKey, action: next, title})
    });
  } catch (_) { /* Markeringen står stadig visuelt; næste reload retter den. */ }
}

function bindFeedback(btn) {
  btn.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); bindFeedbackClick(btn); });
}

function resetFilters() {
  activeCats.clear(); activePeriod = 'all'; priceMin = 0; priceMax = Infinity; endingOnly = false;
  document.querySelectorAll('.chip[data-cat]').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.chip[data-period]').forEach(b =>
    b.classList.toggle('active', b.dataset.period === 'all'));
  const ec = document.getElementById('ending-chip');
  if (ec) ec.classList.remove('active');
  const pmin = document.getElementById('price-min'), pmax = document.getElementById('price-max');
  if (pmin) pmin.value = ''; if (pmax) pmax.value = '';
  applyFilters();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.chip[data-cat]').forEach(btn => {
    btn.addEventListener('click', () => {
      const cat = btn.dataset.cat;
      activeCats.has(cat) ? (activeCats.delete(cat), btn.classList.remove('active'))
                          : (activeCats.add(cat), btn.classList.add('active'));
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
  const ec = document.getElementById('ending-chip');
  if (ec) ec.addEventListener('click', () => {
    endingOnly = !endingOnly;
    ec.classList.toggle('active', endingOnly);
    applyFilters();
  });
  document.querySelectorAll('.sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      activeSort = btn.dataset.sort;
      document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applySort();
    });
  });
  const pmin = document.getElementById('price-min'), pmax = document.getElementById('price-max');
  if (pmin) pmin.addEventListener('input', () => { priceMin = parseInt(pmin.value) || 0; applyFilters(); });
  if (pmax) pmax.addEventListener('input', () => { priceMax = parseInt(pmax.value) || Infinity; applyFilters(); });
  const reset = document.getElementById('reset-btn');
  if (reset) reset.addEventListener('click', resetFilters);
  document.querySelectorAll('.fb-btn').forEach(bindFeedback);
  applyFilters();
});
"""

# ---------------------------------------------------------------------------
# Hjælpere
# ---------------------------------------------------------------------------

def _escape(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")
    )


def _parse_dt(value: str | None) -> datetime | None:
    """ISO-streng til aware datetime. Naive strenge antages at være UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _ts(value: str | None) -> int:
    dt = _parse_dt(value)
    return int(dt.timestamp()) if dt else 0


def _rel_past(value: str | None) -> tuple[str, str]:
    """Hvor længe siden noget skete."""
    dt = _parse_dt(value)
    if dt is None:
        return "", ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    full = dt.astimezone().strftime("%d/%m %H:%M")
    if secs < 120:
        return "lige nu", full
    if secs < 3600:
        return f"{secs // 60} min siden", full
    if secs < 86400:
        return f"{secs // 3600} t siden", full
    return f"{secs // 86400} dage siden", full


def _time_left(ends_at: str | None) -> tuple[str, str, int]:
    """Returnér (tekst, css-klasse, sekunder tilbage).

    Sekunder er 0 når lot'et er slut eller uden sluttidspunkt, så JavaScript kan
    sortere de aktive først uden at kende formatet.
    """
    dt = _parse_dt(ends_at)
    if dt is None:
        return "", "", 0

    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    full = dt.astimezone().strftime("%d/%m %H:%M")

    if secs <= 0:
        past = abs(secs)
        if past < 3600:
            return f"Sluttede {past // 60} min siden", "ended", 0
        if past < 86400:
            return f"Sluttede {past // 3600} t siden", "ended", 0
        return f"Sluttede {past // 86400} dage siden", "ended", 0

    # Timer og minutter under et døgn. Ren timevisning trunkerer 2t59m til
    # "2 t", hvilket ser ud som om der er en time mindre tilbage end der er.
    if secs < 3600:
        return f"Slutter om {secs // 60} min", "urgent", secs
    if secs < SOON_HOURS * 3600:
        hours, minutes = secs // 3600, (secs % 3600) // 60
        text = f"Slutter om {hours} t" + (f" {minutes} min" if minutes else "")
        return text, ("urgent" if secs < URGENT_HOURS * 3600 else "soon"), secs
    days, hours = secs // 86400, (secs % 86400) // 3600
    text = f"Slutter om {days} dag" + ("e" if days != 1 else "")
    if hours:
        text += f" {hours} t"
    return text, "", secs


def _date_label(value: str | None) -> str:
    dt = _parse_dt(value)
    if dt is None:
        return "Ukendt"
    d = dt.date()
    today = datetime.now(timezone.utc).date()
    delta = (today - d).days
    if delta == 0:
        return "I dag"
    if delta == 1:
        return "I går"
    if delta < 7:
        return f"{delta} dage siden"
    return d.strftime("%-d. %B %Y") if delta > 300 else d.strftime("%-d. %B")


def _kr(value: int | None) -> str:
    if not value:
        return "–"
    return f"{value:,} kr".replace(",", ".")


def _ro_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rw_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))

# ---------------------------------------------------------------------------
# Forespørgsler
# ---------------------------------------------------------------------------

def _notification_rows(conn: sqlite3.Connection, limit: int = 250) -> list[sqlite3.Row]:
    """Sendte fund, nyeste først.

    ``image_url`` vælges kun hvis kolonnen findes, så dashboardet også kan køre
    mod en database der endnu ikke er migreret.
    """
    image_col = "l.image_url" if _has_column(conn, "lots", "image_url") else "'' AS image_url"
    return conn.execute(
        f"""
        SELECT n.lot_id, n.category_key, n.sent_at, n.cost,
               l.title, l.url, l.auction_title, l.ends_at, l.lot_number,
               l.first_bid, l.last_bid, {image_col},
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


def _split_by_end(rows: list[sqlite3.Row]) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Del i (aktive, udløbne inden for vinduet).

    Filtreringen sker i Python, fordi ``ends_at`` er ISO med tidszone-offset og
    ikke kan sammenlignes som streng med SQLites ``datetime('now')``.
    """
    now = datetime.now(timezone.utc)
    active: list[sqlite3.Row] = []
    expired: list[sqlite3.Row] = []
    for row in rows:
        dt = _parse_dt(row["ends_at"])
        if dt is None or dt > now:
            active.append(row)
            continue
        age_hours = (now - dt).total_seconds() / 3600
        if age_hours <= EXPIRED_WINDOW_HOURS:
            expired.append(row)
    expired.sort(key=lambda r: _parse_dt(r["ends_at"]) or now, reverse=True)
    return active, expired


def _pending_reviews(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
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
    result: dict[str, int] = {}
    for table in ("lots", "notifications"):
        result[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    result["pending_review"] = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE digested_at IS NULL"
    ).fetchone()[0]
    try:
        result["marked"] = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    except sqlite3.OperationalError:
        result["marked"] = 0
    return result


def _last_run(conn: sqlite3.Connection) -> tuple[str, bool]:
    """(tekst, er_forældet). Forældet = mere end 45 min siden."""
    row = conn.execute(
        "SELECT finished_at FROM runs WHERE finished_at IS NOT NULL "
        "ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return "ingen kørsel endnu", True
    dt = _parse_dt(row[0])
    if dt is None:
        return str(row[0]), True
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    stale = mins > 45
    if mins < 2:
        return "opdateret lige nu", stale
    if mins < 60:
        return f"opdateret {mins} min siden", stale
    hours = mins // 60
    return f"opdateret {hours} t siden", stale


def _save_feedback(db_path: str, lot_id: str, category_key: str, action: str, title: str) -> None:
    """Gem eller fjern en markering. Tom ``action`` sletter rækken."""
    conn = _rw_conn(db_path)
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

# ---------------------------------------------------------------------------
# Kort
# ---------------------------------------------------------------------------

def _card(row: sqlite3.Row) -> str:
    keys = row.keys()
    lot_id = _escape(row["lot_id"] or "")
    cat = _escape(row["category_key"] or "")
    title = _escape(row["title"] or f"Lot {lot_id}")
    url = row["url"] or ""
    cost = row["cost"] or 0
    sent_ts = _ts(row["sent_at"])
    feedback = _escape(row["feedback_action"] if "feedback_action" in keys else "")

    time_text, time_cls, ends_in = _time_left(row["ends_at"] if "ends_at" in keys else None)

    image = row["image_url"] if "image_url" in keys else ""
    if image and str(image).startswith("http"):
        thumb = (
            f"<div class='thumb'><img src='{_escape(image)}' alt='' loading='lazy' "
            f"referrerpolicy='no-referrer' "
            f"onerror=\"this.parentNode.innerHTML='&lt;div class=thumb-empty&gt;📦&lt;/div&gt;'\">"
            f"</div>"
        )
    else:
        thumb = "<div class='thumb'><div class='thumb-empty'>📦</div></div>"

    # Prisstigning siden første observation gør det synligt at andre byder med.
    rise = ""
    if "first_bid" in keys and "last_bid" in keys:
        first, last = row["first_bid"], row["last_bid"]
        if first is not None and last is not None and last > first:
            rise = f"<span class='price-rise'>+{_kr(last - first)} siden første bud</span>"

    price_note = "" if (row["last_bid"] if "last_bid" in keys else None) else \
        "<span class='price-note'>estimat</span>"

    lot_nr = ""
    if "lot_number" in keys and row["lot_number"]:
        lot_nr = f"<span>Lot {_escape(row['lot_number'])}</span>"

    sent_rel, sent_full = _rel_past(row["sent_at"])
    auction = _escape(row["auction_title"] or "")

    title_block = (
        f"<a href='{_escape(url)}' target='_blank' rel='noopener'>"
        f"<div class='title'>{title}</div></a>"
        if url else f"<div class='title'>{title}</div>"
    )

    time_block = (
        f"<span class='time-left {time_cls}'>{_escape(time_text)}</span>"
        if time_text else ""
    )

    def fb(action: str, label: str, tip: str) -> str:
        active = " active" if feedback == action else ""
        return (
            f"<button class='fb-btn{active}' data-action='{action}' "
            f"data-lot-id='{lot_id}' data-cat-key='{cat}' title='{tip}'>{label}</button>"
        )

    return (
        f"<div class='card' data-cat='{cat}' data-ts='{sent_ts}' data-price='{cost}' "
        f"data-endsin='{ends_in}' data-lot='{lot_id}' data-feedback='{feedback}'>"
        f"{thumb}"
        f"<div class='body'>"
        f"{title_block}"
        f"<div class='price-row'><span class='price'>{_kr(cost)}</span>{rise}{price_note}</div>"
        f"<div class='meta'>"
        f"<span class='cat-tag'>{cat}</span>"
        f"{time_block}{lot_nr}"
        f"<span title='Sendt {sent_full}'>{_escape(sent_rel)}</span>"
        f"</div>"
        f"<div class='auction-name' title='{auction}'>{auction}</div>"
        f"<div class='fb-row'>"
        f"{fb('skip', '✕ Ikke interes.', 'Ikke interesseret')}"
        f"{fb('bid', '📌 Budt', 'Jeg har budt på det')}"
        f"{fb('bought', '✅ Købt', 'Jeg købte det')}"
        f"</div>"
        f"</div></div>"
    )


def _grouped_cards(rows: list[sqlite3.Row]) -> str:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(_date_label(row["sent_at"]), []).append(row)
    return "".join(
        f"<div class='date-group'><div class='date-label'>{_escape(label)}</div>"
        + "".join(_card(r) for r in items)
        + "</div>"
        for label, items in groups.items()
    )


def _categories(rows: list[sqlite3.Row]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        cat = row["category_key"] or ""
        if cat and cat not in seen:
            seen.add(cat)
            out.append(cat)
    return sorted(out)


def _review_section(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return ""
    cards = []
    for row in rows:
        title = _escape(row["title"] or "Ukendt titel")
        url = row["url"] or ""
        block = (
            f"<a href='{_escape(url)}' target='_blank' rel='noopener'>"
            f"<div class='title'>{title}</div></a>"
            if url else f"<div class='title'>{title}</div>"
        )
        reason = _escape(row["reason"] or "")
        rel, _ = _rel_past(row["created_at"])
        cards.append(
            f"<div class='card'><div class='body'>"
            f"<div><span class='tag-maybe'>måske</span></div>"
            f"{block}"
            f"<div class='meta'>{reason}</div>"
            f"<div class='auction-name'>{_escape(rel)}</div>"
            f"</div></div>"
        )
    return (
        f"<section><h2>Til gennemsyn <span class='count'>{len(rows)}</span></h2>"
        + "".join(cards)
        + "</section>"
    )

# ---------------------------------------------------------------------------
# Sidestruktur
# ---------------------------------------------------------------------------

def _header(active: str, pulse: str, stale: bool) -> str:
    def link(href: str, label: str, key: str) -> str:
        cls = "nav-link active" if active == key else "nav-link"
        return f"<a href='{href}' class='{cls}'>{label}</a>"
    dot = "dot stale" if stale else "dot"
    return (
        "<header>"
        "<h1>🏷 Auktionshuset Hunter</h1>"
        + link("/", "Fund", "main")
        + link("/expired", "Udløbet", "expired")
        + f"<span class='pulse'><span class='{dot}'></span>{_escape(pulse)}</span>"
        "</header>"
    )


def _stats(counts: dict[str, int], active_count: int) -> str:
    tiles = [
        f"<div class='stat'><strong>{counts['lots']}</strong>lots set</div>",
        f"<div class='stat accent'><strong>{active_count}</strong>aktive fund</div>",
        f"<div class='stat'><strong>{counts['notifications']}</strong>sendt i alt</div>",
    ]
    if counts.get("marked"):
        tiles.append(f"<div class='stat'><strong>{counts['marked']}</strong>markeret</div>")
    if counts.get("pending_review"):
        tiles.append(
            f"<div class='stat'><strong>{counts['pending_review']}</strong>til gennemsyn</div>"
        )
    return "<div class='stats-bar'>" + "".join(tiles) + "</div>"


def _controls(categories: list[str], *, with_period: bool, with_ending: bool) -> str:
    chips = "".join(
        f"<button class='chip' data-cat='{_escape(c)}'>{_escape(c)}</button>"
        for c in categories
    )
    rows = [
        "<div class='filter-row'>"
        f"<span class='filter-label'>Kategori</span>"
        f"{chips or '<span class=&#39;filter-label&#39;>ingen endnu</span>'}"
        "</div>"
    ]

    second: list[str] = ["<div class='filter-row'>"]
    if with_period:
        second.append(
            "<span class='filter-label'>Sendt</span>"
            "<button class='chip active' data-period='all'>Alle</button>"
            "<button class='chip' data-period='today'>I dag</button>"
            "<button class='chip' data-period='week'>7 dage</button>"
            "<div class='filter-sep'></div>"
        )
    if with_ending:
        second.append(
            "<button class='chip urgent' id='ending-chip'>⏰ Slutter &lt; 24 t</button>"
            "<div class='filter-sep'></div>"
        )
    second.append(
        "<span class='filter-label'>Pris</span>"
        "<div class='price-inputs'>"
        "<input id='price-min' type='number' min='0' placeholder='Fra' inputmode='numeric'>"
        "<span>–</span>"
        "<input id='price-max' type='number' min='0' placeholder='Til' inputmode='numeric'>"
        "<span>kr</span>"
        "</div>"
        "<button class='reset-btn' id='reset-btn'>Nulstil</button>"
        "</div>"
    )
    rows.append("".join(second))

    sorts = [("newest", "Nyeste"), ("oldest", "Ældste")]
    if with_ending:
        sorts.append(("ending", "Slutter først"))
    sorts += [("price_desc", "Pris ↓"), ("price_asc", "Pris ↑")]
    buttons = "".join(
        "<button class='sort-btn{active}' data-sort='{key}'>{label}</button>".format(
            active=" active" if key == "newest" else "", key=key, label=label
        )
        for key, label in sorts
    )
    rows.append(
        f"<div class='filter-row'><span class='filter-label'>Sorter</span>{buttons}</div>"
    )
    return "<div class='controls'>" + "".join(rows) + "</div>"


def _shell(*, header: str, stats: str, controls: str, body: str) -> str:
    return (
        "<!doctype html><html lang='da'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'>"
        "<title>Auktionshuset Hunter</title>"
        f"<style>{_CSS}</style></head><body>"
        f"{header}{stats}{controls}<main>{body}</main>"
        "<footer>Fund sendes til Discord &middot; markeringer gemmes lokalt til AI-træning</footer>"
        f"<script>{_JS}</script></body></html>"
    )


def _list_body(rows: list[sqlite3.Row], heading: str, empty_text: str, extra: str = "") -> str:
    if not rows:
        return f"<section><h2>{heading}</h2><p class='empty'>{empty_text}</p></section>{extra}"
    return (
        f"<section>"
        f"<h2>{heading} <span class='count'>{len(rows)}</span></h2>"
        f"<div id='visible-count'></div>"
        f"<div id='no-results'>Ingen fund matcher filteret.</div>"
        f"<div id='cards-container'>{_grouped_cards(rows)}</div>"
        f"<div id='flat-list' style='display:none'></div>"
        f"</section>{extra}"
    )

# ---------------------------------------------------------------------------
# Sider
# ---------------------------------------------------------------------------

def _render_main(db_path: str) -> bytes:
    conn = _ro_conn(db_path)
    try:
        rows = _notification_rows(conn)
        active, _expired = _split_by_end(rows)
        counts = _counts(conn)
        pulse, stale = _last_run(conn)
        reviews = _pending_reviews(conn)
        html = _shell(
            header=_header("main", pulse, stale),
            stats=_stats(counts, len(active)),
            controls=_controls(_categories(active), with_period=True, with_ending=True),
            body=_list_body(
                active,
                "Aktive fund",
                "Ingen aktive fund lige nu.",
                extra=_review_section(reviews),
            ),
        )
        return html.encode()
    finally:
        conn.close()


def _render_expired(db_path: str) -> bytes:
    conn = _ro_conn(db_path)
    try:
        rows = _notification_rows(conn)
        active, expired = _split_by_end(rows)
        counts = _counts(conn)
        pulse, stale = _last_run(conn)
        html = _shell(
            header=_header("expired", pulse, stale),
            stats=_stats(counts, len(active)),
            controls=_controls(_categories(expired), with_period=False, with_ending=False),
            body=_list_body(
                expired,
                f"Sluttet inden for {EXPIRED_WINDOW_HOURS} timer",
                f"Ingen lots er sluttet inden for de seneste {EXPIRED_WINDOW_HOURS} timer.",
            ),
        )
        return html.encode()
    finally:
        conn.close()


def _error_page(message: str) -> bytes:
    return _shell(
        header="<header><h1>🏷 Auktionshuset Hunter</h1></header>",
        stats="",
        controls="",
        body=f"<section><p class='empty'>{_escape(message)}</p></section>",
    ).encode()

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    db_path: str = "data/auction_hunter.db"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # type: ignore[override]
        pass

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/favicon.ico":
            self._send(b"", 204, "image/x-icon")
            return
        try:
            if path == "/":
                self._send(_render_main(self.db_path))
            elif path == "/expired":
                self._send(_render_expired(self.db_path))
            else:
                self._send(_error_page("Siden findes ikke."), 404)
        except sqlite3.OperationalError:
            # Databasen findes ikke endnu, eller scraperen har den låst.
            self._send(_error_page(
                "Databasen er ikke klar endnu. Vent på agentens første kørsel."
            ), 503)
        except Exception:
            self._send(_error_page("Uventet fejl ved opslag i databasen."), 500)

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/feedback":
            self._send(b'{"ok":false}', 404, "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 8192:
                raise ValueError("ugyldig længde")
            data = json.loads(self.rfile.read(length))
            lot_id = str(data.get("lot_id") or "")
            category = str(data.get("category_key") or "")
            action = str(data.get("action") or "")
            title = str(data.get("title") or "")[:300]
            if not lot_id or action not in ("skip", "bid", "bought", ""):
                raise ValueError("ugyldigt input")
            _save_feedback(self.db_path, lot_id, category, action, title)
            self._send(b'{"ok":true}', 200, "application/json")
        except Exception:
            self._send(b'{"ok":false}', 400, "application/json")


def serve(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    db_path: str = "data/auction_hunter.db",
) -> None:
    import logging

    _Handler.db_path = db_path
    logging.getLogger(__name__).info("Dashboard paa http://%s:%d", host, port)
    ThreadingHTTPServer((host, port), _Handler).serve_forever()
