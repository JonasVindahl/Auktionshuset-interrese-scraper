"""FastAPI-app for webdashboardet.

Ruterne er bevidst server-renderede: siden skal virke på en telefon uden et
JavaScript-build, og den eneste JavaScript der er, gør filtrering og markering
hurtigere — ikke mulig.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Any, Callable

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from pydantic import BeforeValidator
from fastapi.responses import (
    HTMLResponse, RedirectResponse, Response, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .. import evaluate as evaluate_mod
from .. import images as images_mod
from ..classifier import OpenAICompatibleClient
from ..config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from ..fees import DEFAULT_OPENING_BID, is_vat_exempt
from ..details import FLAG_LABELS
from ..formatting import MONTHS_DA
from ..secrets import SecretError, get_secret
from . import (
    auth,
    chat as chat_mod,
    queries,
    rows as rows_mod,
    search as search_mod,
    similar as similar_mod,
    suggest as suggest_mod,
)
from .formatting import date_label, kr, rel_past, time_left, timestamp
from .yamledit import EditError, InterestsFile, LEVELS

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
DEFAULT_DB = "data/auction_hunter.db"

# Sider i topmenuen: (sti, etiket)
NAV = (
    ("/", "Fund"),
    ("/expired", "Udløbet"),
    ("/mine", "Mine"),
    ("/archive", "Arkiv"),
    ("/interests", "Interesser"),
    ("/chat", "Assistent"),
    ("/stats", "Statistik"),
    ("/drift", "Drift"),
)


def _blank_to_none(value: Any) -> Any:
    """Tom streng betyder 'ikke udfyldt', ikke 'ugyldigt tal'.

    En HTML-formular sender hvert felt med, også de tomme. Uden denne
    oversættelse svarer FastAPI 422 på en søgning hvor prisfelterne bare står
    tomme — altså på den helt almindelige brug af søgeformularen.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


# Et heltalsfelt der må stå tomt i en formular.
OptionalInt = Annotated[int | None, BeforeValidator(_blank_to_none)]


def db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB)


def ro_conn(db: str) -> sqlite3.Connection:
    """Read-only forbindelse til visning.

    Kan filen ikke åbnes, returneres en tom database i hukommelsen. Så viser
    siderne en tom tilstand i stedet for at fejle: det er et helt normalt
    første kørselsforløb at starte dashboardet før agenten har skrevet noget.
    Healthz-ruten bruger bevidst queries.ro_conn direkte, så den stadig melder
    fra om en manglende database.
    """
    try:
        return queries.ro_conn(db)
    except sqlite3.Error:
        log.warning("Kunne ikke åbne databasen %s, viser tom tilstand", db)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn


def config_path() -> str | None:
    return os.environ.get("CONFIG_PATH")


def llm_client() -> OpenAICompatibleClient | None:
    """Klient til chat-assistenten, eller None hvis ingen nøgle er sat.

    Nøglen slås op ved hvert kald, så en roteret hemmelighed virker uden
    genstart — samme princip som i agenten.
    """
    try:
        api_key = get_secret("CLASSIFIER_API_KEY") or get_secret("OPENAI_API_KEY")
    except SecretError as exc:
        log.error("Nøglen til assistenten kunne ikke læses: %s", exc)
        return None
    if not api_key:
        return None
    try:
        settings = load_config(config_path()).classifier
        base_url, model = settings.base_url, settings.model
    except ConfigError:
        base_url, model = "https://api.openai.com/v1", "gpt-4o-mini"
    return OpenAICompatibleClient(
        base_url=base_url, api_key=api_key, model=model, timeout=60
    )


# Konfigurationen ændrer sig kun når filen gør, og den slås op ved hvert
# sidekald. Derfor caches de få værdier webben skal bruge, på mtime og størrelse.
_config_cache: dict[str, Any] = {
    "key": None, "labels": {}, "opening_bid": DEFAULT_OPENING_BID,
}


def _config_values() -> tuple[dict[str, str], int]:
    path = config_path()
    try:
        stat = os.stat(path or DEFAULT_CONFIG_PATH)
        key: tuple[Any, ...] = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(path), None, None)

    if _config_cache["key"] == key:
        return dict(_config_cache["labels"]), int(_config_cache["opening_bid"])

    try:
        config = load_config(path)
        labels = {category.key: category.label for category in config.categories}
        opening = config.opening_bid
    except ConfigError:
        # Et tomt kort er et gyldigt svar: siden skal kunne vises, selvom filen
        # mangler eller er i stykker.
        labels, opening = {}, DEFAULT_OPENING_BID

    _config_cache.update(key=key, labels=labels, opening_bid=opening)
    return labels, opening


def category_labels() -> dict[str, str]:
    """Kategorinøgler oversat til de læsbare etiketter fra interesseprofilen."""
    return _config_values()[0]


def opening_bid() -> int:
    """Første bud-grænsen, brugt til at vise hvad et lot uden bud koster."""
    return _config_values()[1]


def archive_url(query: search_mod.SearchQuery, **changes: Any) -> str:
    """Byg et /archive-link ud fra en søgning, med enkelte felter ændret.

    En værdi på None eller "" fjerner filtret. Det giver både de fjernbare
    chips og statustabene, uden at skabelonen skal kende feltnavnene.
    """
    from urllib.parse import urlencode

    params: dict[str, Any] = {
        "q": query.text,
        "min_price": query.min_price,
        "max_price": query.max_price,
        "status": query.status if query.status != "alle" else "",
        "matched": query.matched if query.matched != "alle" else "",
        "category": query.category,
        "auction": query.auction,
        "days_back": query.days_back,
        "sort": query.sort if query.sort != "relevans" else "",
    }
    params.update(changes)
    clean = {key: value for key, value in params.items() if value}
    return "/archive?" + urlencode(clean) if clean else "/archive"


# Status er den filtre ring man oftest skifter mellem, så den ligger som
# faneblade i stedet for i filterpanelet. De er links og virker uden JavaScript.
_STATUS_TABS = (("alle", "Alle"), ("aktive", "Aktive"), ("afsluttede", "Afsluttede"))


def status_tabs(query: search_mod.SearchQuery) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "url": archive_url(query, status="" if key == "alle" else key),
            "active": query.status == key,
        }
        for key, label in _STATUS_TABS
    ]


def _safe_config() -> Any | None:
    """Konfigurationen, eller None hvis filen ikke kan læses."""
    try:
        return load_config(config_path())
    except ConfigError:
        return None


def secret_status(name: str) -> str:
    """Om en hemmelighed er sat. Aldrig værdien, kun tilstanden.

    En hemmelighed der er sat men ikke kan læses er en fejl nogen skal se, ikke
    noget der skal skjules bag "ikke sat".
    """
    try:
        return "sat" if get_secret(name) else "ikke sat"
    except SecretError:
        return "sat, men kunne ikke læses"


# Etiketter til sorterings-chippen, så opsummeringen bliver dansk frem for
# feltnavne.
_ARCHIVE_SORT_LABELS = {
    "nyeste": "nyeste",
    "slutter": "slutter først",
    "pris_op": "laveste pris",
    "pris_ned": "højeste pris",
}


def filter_chips(query: search_mod.SearchQuery) -> list[dict[str, str]]:
    """De aktive arkivfiltre som fjernbare chips.

    Status er ikke med her: den vises som faneblade i stedet. Hver chip bærer
    et link til den samme søgning uden netop det filter, så oprydningen er ét
    klik og virker uden JavaScript.
    """
    labels = category_labels()

    chips: list[dict[str, str]] = []
    if query.text:
        chips.append({"label": f'"{query.text}"', "url": archive_url(query, q="")})
    if query.min_price is not None:
        chips.append({"label": f"fra {kr(query.min_price)}", "url": archive_url(query, min_price=None)})
    if query.max_price is not None:
        chips.append({"label": f"til {kr(query.max_price)}", "url": archive_url(query, max_price=None)})
    if query.matched == "kun_fund":
        chips.append({"label": "kun fund", "url": archive_url(query, matched="")})
    elif query.matched == "kun_ikke_fund":
        chips.append({"label": "kun ikke-fund", "url": archive_url(query, matched="")})
    if query.category:
        chips.append({
            "label": labels.get(query.category, query.category),
            "url": archive_url(query, category=""),
        })
    if query.auction:
        chips.append({
            "label": f"fra {query.auction}",
            "url": archive_url(query, auction=""),
        })
    if query.days_back:
        unit = "dag" if query.days_back == 1 else "dage"
        chips.append({
            "label": f"set inden for {query.days_back} {unit}",
            "url": archive_url(query, days_back=None),
        })
    if query.sort != "relevans":
        chips.append({
            "label": "sorteret: " + _ARCHIVE_SORT_LABELS.get(query.sort, query.sort),
            "url": archive_url(query, sort=""),
        })
    return chips


# Excel og Sheets evaluerer en celle der begynder med = + - @ som en formel.
# Titel og lot_id kommer fra auktionshusets HTML, saa de skal neutraliseres.
_CSV_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in _CSV_FORMULA_STARTS:
        return "'" + value
    return value


def pct(value: float) -> str:
    """Procent med dansk decimalseparator. round(1) giver '70.0 %' på engelsk."""
    return f"{value:.1f}".replace(".", ",")


def safe_next(target: str) -> str:
    """Kun interne stier, så et login-link ikke kan sende folk videre.

    Bruges både når login-formularen vises og når den indsendes. Uden den
    kunne GET /login?next=https://ondsindet.example videresende direkte.
    """
    return target if target.startswith("/") and not target.startswith("//") else "/"


# Konservativ CSP. Den tillader kun script fra /static, billeder fra
# auktionshuset og data-URI'er, og forbyder at siden rammes ind i en anden
# (clickjacking). 'unsafe-inline' er nødvendigt for style, fordi et par
# skabeloner sætter bredder direkte som style-attribut.
_CSP = (
    "default-src 'self'; img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "form-action 'self'; base-uri 'self'; frame-ancestors 'none'"
)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Auktionshuset Hunter",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,   # ingen offentlig rute-enumeration
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.session_secret(),
        max_age=auth.SESSION_MAX_AGE,
        same_site="lax",
        https_only=False,      # kører typisk på LAN uden TLS
    )
    @app.middleware("http")
    async def sikkerhedsheadere(request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        # Siderne kan redigere interesseprofilen, så de må ikke havne i en cache.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["kr"] = kr
    templates.env.filters["pct"] = pct
    templates.env.filters["date_label"] = date_label
    templates.env.filters["timestamp"] = timestamp
    templates.env.globals["time_left"] = time_left
    templates.env.globals["rel_past"] = rel_past
    templates.env.globals["is_vat_exempt"] = is_vat_exempt
    templates.env.globals["nav"] = NAV

    def with_series(rows: list[dict]) -> list[dict]:
        """Læg prisforløbet på de forberedte rækker, i ét opslag."""
        if not rows:
            return rows
        conn = ro_conn(db_path())
        try:
            series = queries.price_series(conn, [r["lot_id"] for r in rows])
        finally:
            conn.close()
        for row in rows:
            row["series"] = series.get(row["lot_id"], [])
        return rows

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        """Render en side med det fælles sidehoved allerede udfyldt."""
        conn = ro_conn(db_path())
        try:
            pulse, stale = queries.last_run(conn)
            counts = queries.counts(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request, name,
            {"pulse": pulse, "stale": stale, "counts": counts,
             "category_labels": category_labels(),
             "path": request.url.path, **context},
        )

    # -- login -------------------------------------------------------------

    # Dashboardet har én adgangskode, så et hurtigt ordbogsangreb er den
    # realistiske trussel. Tælleren lever i processen, hvilket er nok fordi
    # uvicorn kører én worker. Bag en reverse proxy er client.host proxyens
    # adresse, så grænsen bliver global frem for per klient; det er
    # acceptabelt for et enkeltbruger-dashboard.
    LOGIN_MAX_ATTEMPTS = 5
    LOGIN_WINDOW_SECONDS = 900
    _login_failures: dict[str, list[float]] = {}

    def _login_key(request: Request) -> str:
        return request.client.host if request.client else "ukendt"

    def _recent_failures(key: str) -> list[float]:
        now = time.monotonic()
        recent = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        if recent:
            _login_failures[key] = recent
        else:
            _login_failures.pop(key, None)
        return recent

    def require_login(request: Request) -> None:
        if not auth.auth_required():
            return
        if not request.session.get(auth.SESSION_KEY):
            raise HTTPException(status_code=401)

    @app.exception_handler(401)
    async def unauthorized(request: Request, _exc: Exception) -> RedirectResponse:
        return RedirectResponse(f"/login?next={safe_next(request.url.path)}", HTTP_303_SEE_OTHER)

    @app.exception_handler(auth.AuthConfigError)
    async def auth_misconfigured(request: Request, _exc: Exception) -> HTMLResponse:
        """Fejl lukket: en ulæselig adgangskode må ikke åbne dashboardet."""
        return HTMLResponse(
            "<!doctype html><html lang=da><meta charset=utf-8>"
            "<title>Dashboardet er ikke konfigureret</title>"
            "<h1>Dashboardet er ikke konfigureret</h1>"
            "<p>WEB_PASSWORD er sat, men kunne ikke læses, så adgang nægtes. "
            "Ret stien i WEB_PASSWORD_FILE (eller den variabel "
            "WEB_PASSWORD_FROM_ENV peger på) og genstart.</p>",
            status_code=503,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/") -> Any:
        if not auth.auth_required() or request.session.get(auth.SESSION_KEY):
            return RedirectResponse(safe_next(next), HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": None}
        )

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request, password: str = Form(""), next: str = Form("/")
    ) -> Any:
        key = _login_key(request)
        if len(_recent_failures(key)) >= LOGIN_MAX_ATTEMPTS:
            return templates.TemplateResponse(
                request, "login.html",
                {"next": next, "error": "For mange forsøg. Prøv igen om et kvarter."},
                status_code=429,
            )
        if auth.check_password(password):
            _login_failures.pop(key, None)
            request.session[auth.SESSION_KEY] = True
            return RedirectResponse(safe_next(next), HTTP_303_SEE_OTHER)
        _login_failures.setdefault(key, []).append(time.monotonic())
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next, "error": "Forkert adgangskode."},
            status_code=401,
        )

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", HTTP_303_SEE_OTHER)

    # -- fund --------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def finds(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        conn = ro_conn(db_path())
        try:
            rows = queries.notifications(conn)
            active, _expired = queries.split_by_end(rows)
            reviews = queries.pending_reviews(conn)
        finally:
            conn.close()
        categories = sorted({r["category_key"] for r in active if r["category_key"]})
        prepared = with_series(rows_mod.prepare_all(active, db_path=db_path(), opening_bid=opening_bid()))
        return page(
            request, "finds.html",
            rows=prepared, groups=rows_mod.group_by_date(prepared),
            reviews=reviews, categories=categories,
            heading="Aktive fund",
            empty="Ingen aktive fund lige nu.",
            show_ending=True, show_period=True,
        )

    @app.get("/expired", response_class=HTMLResponse)
    def expired(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        conn = ro_conn(db_path())
        try:
            rows = queries.notifications(conn)
            _active, gone = queries.split_by_end(rows)
        finally:
            conn.close()
        categories = sorted({r["category_key"] for r in gone if r["category_key"]})
        prepared = with_series(rows_mod.prepare_all(gone, db_path=db_path(), opening_bid=opening_bid()))
        return page(
            request, "finds.html",
            rows=prepared, groups=rows_mod.group_by_date(prepared),
            reviews=[], categories=categories,
            heading=f"Sluttet inden for {queries.EXPIRED_WINDOW_HOURS} timer",
            empty=f"Ingen lots er sluttet inden for de seneste "
                  f"{queries.EXPIRED_WINDOW_HOURS} timer.",
            show_ending=False, show_period=False,
        )

    # -- mine markeringer --------------------------------------------------

    @app.get("/mine", response_class=HTMLResponse)
    def mine(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        """Det du selv har markeret. Webben skriver til feedback, og her vises
        den igen sammen med hvad lot'ene koster."""
        conn = ro_conn(db_path())
        try:
            rows = queries.marked_lots(conn)
        finally:
            conn.close()

        prepared = with_series(rows_mod.prepare_all(
            rows, seen_field="created_at", db_path=db_path(), opening_bid=opening_bid()
        ))
        groups = [
            (key, label, [r for r in prepared if r["feedback"] == key])
            for key, label in (("bought", "Købt"), ("bid", "Budt"),
                               ("watch", "Følger"), ("skip", "Afvist"))
        ]
        groups = [(key, label, items) for key, label, items in groups if items]
        committed = [r for r in prepared if r["feedback"] in ("bid", "bought")]
        total = sum(r["cost"] or 0 for r in committed)

        # Månedligt resumé: hvad er der budt og købt for, måned for måned.
        month_of = {raw["lot_id"]: (raw["created_at"] or "")[:7] for raw in rows}
        months: dict[str, dict[str, int]] = {}
        for row in committed:
            month = month_of.get(row["lot_id"], "")
            if not month:
                continue
            entry = months.setdefault(month, {"n": 0, "sum": 0})
            entry["n"] += 1
            entry["sum"] += row["cost"] or 0
        monthly = [
            {
                "key": month,
                "label": f"{MONTHS_DA[int(month[5:7]) - 1]} {month[:4]}",
                "n": data["n"],
                "sum": data["sum"],
            }
            for month, data in sorted(months.items(), reverse=True)
            if len(month) == 7 and month[5:7].isdigit()
        ]

        return page(
            request, "mine.html",
            groups=groups, count=len(prepared), committed=len(committed),
            total=total, monthly=monthly,
        )

    @app.post("/feedback")
    def feedback(
        payload: dict, _: None = Depends(require_login)
    ) -> dict:
        lot_id = str(payload.get("lot_id") or "")
        if not lot_id:
            raise HTTPException(400, "lot_id mangler")
        action = str(payload.get("action") or "")
        if action and action not in queries.ACTIONS:
            raise HTTPException(400, "ukendt handling")
        queries.save_feedback(
            db_path(), lot_id,
            str(payload.get("category_key") or ""),
            action,
            str(payload.get("title") or "")[:300],
        )
        return {"ok": True}

    # -- arkiv -------------------------------------------------------------

    @app.get("/archive", response_class=HTMLResponse)
    def archive(
        request: Request,
        q: str = Query("", max_length=200),
        min_price: OptionalInt = None,
        max_price: OptionalInt = None,
        status: str = "alle",
        matched: str = "alle",
        category: str = "",
        days_back: OptionalInt = None,
        sort: str = "relevans",
        page_no: Annotated[
            int | None, BeforeValidator(_blank_to_none), Query(alias="page")
        ] = 1,
        _: None = Depends(require_login),
    ) -> HTMLResponse:
        conn = ro_conn(db_path())
        try:
            result = search_mod.search(conn, search_mod.SearchQuery(
                text=q, min_price=min_price, max_price=max_price,
                status=status, matched=matched, category=category,
                days_back=days_back, sort=sort, page=page_no or 1,
            ))
            stats = search_mod.archive_stats(conn)
            categories = search_mod.categories_seen(conn)
            auctions = queries.auctions_seen(conn)
        finally:
            conn.close()

        def page_url(target: int) -> str:
            """Link til en anden side med de samme filtre."""
            from urllib.parse import urlencode
            params = {
                k: v for k, v in {
                    "q": q, "min_price": min_price, "max_price": max_price,
                    "status": status if status != "alle" else None,
                    "matched": matched if matched != "alle" else None,
                    "category": category or None, "days_back": days_back,
                    "sort": sort if sort != "relevans" else None,
                    "page": target,
                }.items() if v
            }
            return "/archive?" + urlencode(params)

        return page(
            request, "archive.html",
            result=result,
            rows=with_series(rows_mod.prepare_all(result.rows, seen_field="first_seen", db_path=db_path(), opening_bid=opening_bid())),
            archive=stats, categories=categories, page_url=page_url,
            filters=filter_chips(result.query),
            status_tabs=status_tabs(result.query),
            auctions=auctions,
            match_choices=search_mod.MATCH_CHOICES,
            sort_choices=search_mod.SORT_CHOICES,
        )

    # -- interesser --------------------------------------------------------

    def interests_file() -> InterestsFile:
        path = config_path() or "config/interests.yml"
        return InterestsFile(path)

    @app.get("/interests", response_class=HTMLResponse)
    def interests(
        request: Request,
        message: str = "",
        error: str = "",
        test: str = "",
        cat: str = "",
        ai: bool = False,
        _: None = Depends(require_login),
    ) -> HTMLResponse:
        try:
            handle = interests_file()
            data = handle.data()
            categories = [
                {
                    "key": key,
                    "label": (data["categories"].get(key) or {}).get("label", key),
                    "emoji": (data["categories"].get(key) or {}).get("emoji", ""),
                    "max_price": (data["categories"].get(key) or {}).get("max_price"),
                    "levels": {lvl: handle.keywords(key, lvl) for lvl in LEVELS},
                }
                for key in handle.categories()
            ]
            excludes = handle.excludes()
            read_error = ""
        except (OSError, EditError) as exc:
            categories, excludes, read_error = [], [], str(exc)

        trace = None
        explanation = ""
        if test.strip():
            try:
                config = load_config(config_path())
                trace, explanation = chat_mod.explain(config, llm_client(), test)
            except ConfigError as exc:
                error = error or f"Konfigurationen kunne ikke læses: {exc}"

        # Forslag fra feedbacken. Ren regelbaseret analyse, ingen model: den
        # skal være billig og forudsigelig, og den foreslår aldrig selv et nyt
        # nøgleord. Intet anvendes uden et klik.
        suggestions: list = []
        unmatched: list = []
        suggest_config = None
        try:
            suggest_config = load_config(config_path())
            suggest_conn = queries.ro_conn(db_path())
            try:
                suggestions = suggest_mod.noisy_keywords(suggest_conn, suggest_config)
                unmatched = suggest_mod.unmatched_marks(suggest_conn, suggest_config)
                # Facitliste-porten: vis om et forslag ville bryde et hårdt krav.
                suggestions = suggest_mod.annotate_impact(
                    suggestions, suggest_config, evaluate_mod.load_corpus()
                )
            finally:
                suggest_conn.close()
        except (ConfigError, sqlite3.Error, OSError):
            pass

        # Modellens forslag køres kun når man beder om det: det koster et kald,
        # og siden skal ikke spørge af sig selv hver gang man kigger forbi.
        ai_suggestions: list = []
        if ai and unmatched and suggest_config is not None:
            client = llm_client()
            if client is not None:
                titles = [row["title"] for row in unmatched]
                entries = chat_mod.suggest_keywords(suggest_config, client, titles)
                ai_suggestions = suggest_mod.from_model(entries, suggest_config, titles)
                ai_suggestions = suggest_mod.annotate_impact(
                    ai_suggestions, suggest_config, evaluate_mod.load_corpus()
                )

        # To-punkts-editoren viser én kategori ad gangen. Uden et gyldigt valg
        # falder den tilbage til den første, så siden aldrig står tom.
        selected = next(
            (c for c in categories if c["key"] == cat.strip()),
            categories[0] if categories else None,
        )

        return page(
            request, "interests.html",
            categories=categories, selected=selected,
            excludes=excludes, levels=LEVELS,
            message=message, error=error or read_error,
            test=test, trace=trace, explanation=explanation,
            suggestions=suggestions, unmatched=unmatched,
            ai=ai, ai_suggestions=ai_suggestions,
            editable=os.access(config_path() or "config/interests.yml", os.W_OK),
        )

    def _interests_redirect(
        message: str = "", error: str = "", cat: str = ""
    ) -> RedirectResponse:
        """Tilbage til samme kategori, så en rettelse ikke flytter brugeren."""
        from urllib.parse import urlencode
        params = {k: v for k, v in (
            ("message", message), ("error", error), ("cat", cat)
        ) if v}
        suffix = ("?" + urlencode(params)) if params else ""
        return RedirectResponse(f"/interests{suffix}", HTTP_303_SEE_OTHER)

    @app.post("/interests/keyword")
    def interests_keyword(
        action: str = Form(...),
        category: str = Form(...),
        level: str = Form(...),
        keyword: str = Form(...),
        _: None = Depends(require_login),
    ) -> RedirectResponse:
        try:
            handle = interests_file()
            if action == "add":
                changed = handle.add_keyword(category, level, keyword)
                note = (f"Tilføjede '{keyword}' til {category}.{level}."
                        if changed else f"'{keyword}' stod der allerede.")
            elif action == "remove":
                changed = handle.remove_keyword(category, level, keyword)
                note = (f"Fjernede '{keyword}' fra {category}.{level}."
                        if changed else f"Fandt ikke '{keyword}'.")
            else:
                return _interests_redirect(error="Ukendt handling.")
            if changed:
                handle.save()
            return _interests_redirect(message=note, cat=category)
        except (EditError, OSError) as exc:
            return _interests_redirect(error=str(exc), cat=category)

    @app.post("/interests/exclude")
    def interests_exclude(
        action: str = Form(...),
        keyword: str = Form(...),
        cat: str = Form(""),
        _: None = Depends(require_login),
    ) -> RedirectResponse:
        try:
            handle = interests_file()
            if action == "add":
                changed = handle.add_exclude(keyword)
                note = (f"Udelukker nu '{keyword}'."
                        if changed else f"'{keyword}' var allerede udelukket.")
            elif action == "remove":
                changed = handle.remove_exclude(keyword)
                note = (f"Fjernede udelukkelsen af '{keyword}'."
                        if changed else f"Fandt ikke '{keyword}'.")
            else:
                return _interests_redirect(error="Ukendt handling.")
            if changed:
                handle.save()
            return _interests_redirect(message=note, cat=cat.strip())
        except (EditError, OSError) as exc:
            return _interests_redirect(error=str(exc), cat=cat.strip())

    @app.post("/interests/price")
    def interests_price(
        category: str = Form(...),
        max_price: str = Form(""),
        _: None = Depends(require_login),
    ) -> RedirectResponse:
        # Feltet kan stå tomt, og et tomt felt er ikke en serverfejl.
        try:
            limit = int(max_price.strip())
        except ValueError:
            return _interests_redirect(error="Prisloftet skal være et helt tal.")
        if not 0 < limit <= 10_000_000:
            return _interests_redirect(error="Prisloftet skal være mellem 1 og 10.000.000.")
        try:
            handle = interests_file()
            handle.set_scalar(("categories", category, "max_price"), limit)
            handle.save()
            return _interests_redirect(
                message=f"Prisloftet for {category} er nu {kr(limit)}.", cat=category
            )
        except (EditError, OSError) as exc:
            return _interests_redirect(error=str(exc), cat=category)

    # -- assistent ---------------------------------------------------------

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(
        request: Request,
        q: str = Query("", max_length=chat_mod.MAX_QUESTION_LENGTH),
        alle: bool = False,
        _: None = Depends(require_login),
    ) -> HTMLResponse:
        answer = None
        # Klienten læses fra konfiguration og miljø, så den hentes én gang
        # pr. sidekald i stedet for to.
        client = llm_client()
        if q.strip():
            conn = ro_conn(db_path())
            try:
                # Kategorinøglerne gives til filterbyggeriet, så modellen kan
                # vælge en kategori i stedet for kun at gætte på ord.
                config = _safe_config()
                keys = [c.key for c in config.categories] if config else None
                answer = chat_mod.ask(conn, client, q, show_all=alle, categories=keys)
            finally:
                conn.close()
        return page(
            request, "chat.html",
            question=q, answer=answer, show_all=alle,
            rows=with_series(rows_mod.prepare_all(answer.rows, seen_field="first_seen", db_path=db_path(), opening_bid=opening_bid())) if answer else [],
            has_key=client is not None,
        )

    # -- statistik ---------------------------------------------------------

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        conn = ro_conn(db_path())
        try:
            context = {
                "by_category": queries.category_breakdown(conn),
                "feedback": queries.feedback_breakdown(conn),
                "runs": queries.run_history(conn, limit=25),
                "risers": queries.price_risers(conn),
                "archive": search_mod.archive_stats(conn),
                "prices": queries.price_summary(conn),
                "bands": queries.price_bands(conn),
                # Analyse af nøgleord kræver profilen. Kan den ikke læses, vises
                # resten af statistikken stadig.
                "noise": queries.keyword_noise(conn, _safe_config()),
            }
        finally:
            conn.close()
        count, total = images_mod.usage(db_path())
        context["images"] = {
            "count": count,
            "mb": round(total / 1024 / 1024, 1),
        }
        return page(request, "stats.html", **context)

    # -- et enkelt lot ------------------------------------------------------

    @app.get("/lot/{lot_id}", response_class=HTMLResponse)
    def lot_page(
        request: Request, lot_id: str, _: None = Depends(require_login)
    ) -> HTMLResponse:
        """Alt om ét lot: prisen, forløbet og hvad samme slags er gået for."""
        conn = ro_conn(db_path())
        try:
            row = queries.lot_detail(conn, lot_id)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(404, "lot findes ikke")

        prepared = with_series(rows_mod.prepare_all(
            [row], seen_field="first_seen", db_path=db_path(), opening_bid=opening_bid()
        ))[0]

        conn = ro_conn(db_path())
        try:
            comps = similar_mod.find(conn, lot_id=lot_id, title=prepared["title"])
        finally:
            conn.close()

        return page(request, "lot.html", row=prepared, comps=comps,
                    FLAG_LABELS=FLAG_LABELS)

    # -- drift -------------------------------------------------------------

    @app.get("/drift", response_class=HTMLResponse)
    def drift(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        """Alt det der ellers kun står i loggen: kørsler, størrelser og tilstand."""
        conn = ro_conn(db_path())
        try:
            tables = queries.table_counts(conn)
            span = queries.memory_span(conn)
            runs = queries.run_history(conn, limit=12)
            pulse, stale = queries.last_run(conn)
            blind_at = queries.meta_value(conn, "blind_alert_at")
            normal_lots = queries.last_normal_lot_count(conn)
            reviews = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE digested_at IS NULL"
            ).fetchone()[0] if queries.has_table(conn, "review_queue") else 0
        finally:
            conn.close()

        db = db_path()
        try:
            db_bytes = os.path.getsize(db)
        except OSError:
            db_bytes = 0
        image_count, image_bytes = images_mod.usage(db)

        config_file = config_path() or "config/interests.yml"
        settings: dict[str, Any] = {
            "path": config_file,
            "readable": True,
            "writable": os.access(config_file, os.W_OK),
            "error": "",
        }
        try:
            config = load_config(config_path())
            settings |= {
                "categories": len(config.categories),
                "keywords": sum(
                    len(c.strong) + len(c.weak) + len(c.brands) + len(c.exact)
                    for c in config.categories
                ),
                "excludes": len(config.exclude),
                "max_price": config.max_price,
                "soft": config.soft_over_budget_factor,
                "opening_bid": config.opening_bid,
                "region": config.source.region_label,
                "ai_enabled": config.classifier.enabled,
                "ai_model": config.classifier.model,
                "ai_base": config.classifier.base_url,
            }
        except ConfigError as exc:
            settings |= {"readable": False, "error": str(exc)}

        ai_key = secret_status("CLASSIFIER_API_KEY")
        if ai_key == "ikke sat":
            ai_key = secret_status("OPENAI_API_KEY")

        return page(
            request, "drift.html",
            pulse=pulse, stale=stale, tables=tables, span=span, runs=runs,
            blind_at=blind_at, normal_lots=normal_lots, reviews=reviews,
            db_bytes=db_bytes, image_count=image_count, image_bytes=image_bytes,
            settings=settings,
            secrets={
                "Discord-webhook": secret_status("DISCORD_WEBHOOK_URL"),
                "AI-noegle": ai_key,
                "Adgangskode": secret_status("WEB_PASSWORD"),
            },
        )

    @app.get("/export/archive.csv")
    def export_archive(_: None = Depends(require_login)) -> StreamingResponse:
        """Hele arkivet som CSV, saa priserne kan undersøges i et regneark.

        Markeringerne har haft deres egen eksport længe; arkivet er den store
        datamængde, og det er den man vil analysere.
        """
        conn = ro_conn(db_path())
        try:
            rows = queries.archive_export(conn)
        finally:
            conn.close()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["lot_id", "titel", "auktion", "lot_nr", "kategori",
                         "bud", "total", "første_bud", "slutter", "set_første_gang",
                         "set_sidst", "url"])
        for row in rows:
            writer.writerow([
                _csv_safe(row["lot_id"]), _csv_safe(row["title"]),
                _csv_safe(row["auction_title"]), row["lot_number"],
                row["category_key"], row["last_bid"] or "", row["last_total"] or "",
                row["first_bid"] or "", row["ends_at"] or "",
                row["first_seen"] or "", row["last_seen"] or "", row["url"] or "",
            ])
        buffer.seek(0)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="arkiv.csv"'},
        )

    @app.get("/export/feedback.csv")
    def export_feedback(_: None = Depends(require_login)) -> StreamingResponse:
        """Markeringer som CSV — træningsdata til AI-trinnet."""
        conn = ro_conn(db_path())
        try:
            rows = queries.feedback_export(conn)
        finally:
            conn.close()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["lot_id", "kategori", "handling", "titel", "pris", "url", "tidspunkt"])
        for row in rows:
            writer.writerow([
                _csv_safe(row["lot_id"]), row["category_key"], row["action"],
                _csv_safe(row["title"]), row["last_total"] or "",
                row["url"] or "", row["created_at"],
            ])
        buffer.seek(0)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="feedback.csv"'},
        )

    @app.get("/image/{lot_id}")
    def lot_image(lot_id: str, _: None = Depends(require_login)) -> Response:
        """Udlever et cachet lot-billede.

        Auktionshuset fjerner billedet når lot'et lukker, så det cachede er
        det eneste der overlever. Findes der intet, svares 404 og skabelonen
        falder tilbage til den levende adresse.
        """
        cached = images_mod.read_cached(db_path(), lot_id)
        if cached is None:
            raise HTTPException(404, "intet cachet billede")
        data, media_type = cached
        return Response(
            content=data,
            media_type=media_type,
            headers={
                # Filen ændrer sig aldrig: den er gemt netop fordi originalen
                # forsvinder.
                "Cache-Control": "public, max-age=604800, immutable",
                "Content-Length": str(len(data)),
            },
        )

    @app.get("/healthz")
    def healthz() -> dict:
        """Til Docker og overvågning. Rammer ikke auktionshuset."""
        try:
            conn = queries.ro_conn(db_path())
            try:
                conn.execute("SELECT 1 FROM lots LIMIT 1").fetchone()
            finally:
                conn.close()
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(503, f"database utilgængelig: {exc}") from exc

    return app


def serve(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    db_path: str = DEFAULT_DB,
) -> None:
    """Start dashboardet. Kaldes fra CLI'en."""
    import uvicorn

    os.environ.setdefault("DB_PATH", db_path)
    try:
        auth_ok = auth.auth_required()
    except auth.AuthConfigError:
        auth_ok = True   # fejler lukket; håndteres pr. request som 503
    if not auth_ok:
        log.warning(
            "WEB_PASSWORD er ikke sat — dashboardet er åbent for alle på "
            "netværket, og det kan redigere interesseprofilen. Sæt WEB_PASSWORD."
        )
    log.info("Dashboard paa http://%s:%d", host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
