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
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from pydantic import BeforeValidator
from fastapi.responses import (
    HTMLResponse, RedirectResponse, Response, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .. import images as images_mod
from ..classifier import OpenAICompatibleClient
from ..config import ConfigError, load_config
from ..secrets import SecretError, get_secret
from . import auth, chat as chat_mod, queries, rows as rows_mod, search as search_mod
from .formatting import date_label, kr, rel_past, time_left, timestamp
from .yamledit import EditError, InterestsFile, LEVELS

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
DEFAULT_DB = "data/auction_hunter.db"

# Sider i topmenuen: (sti, etiket)
NAV = (
    ("/", "Fund"),
    ("/expired", "Udløbet"),
    ("/archive", "Arkiv"),
    ("/interests", "Interesser"),
    ("/chat", "Assistent"),
    ("/stats", "Statistik"),
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


def create_app() -> FastAPI:
    app = FastAPI(title="Auktionshuset Hunter", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.session_secret(),
        max_age=auth.SESSION_MAX_AGE,
        same_site="lax",
        https_only=False,      # kører typisk på LAN uden TLS
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["kr"] = kr
    templates.env.filters["date_label"] = date_label
    templates.env.filters["timestamp"] = timestamp
    templates.env.globals["time_left"] = time_left
    templates.env.globals["rel_past"] = rel_past
    templates.env.globals["nav"] = NAV

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        """Render en side med det fælles sidehoved allerede udfyldt."""
        conn = queries.ro_conn(db_path())
        try:
            pulse, stale = queries.last_run(conn)
            counts = queries.counts(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request, name,
            {"pulse": pulse, "stale": stale, "counts": counts,
             "path": request.url.path, **context},
        )

    # -- login -------------------------------------------------------------

    def require_login(request: Request) -> None:
        if not auth.auth_required():
            return
        if not request.session.get(auth.SESSION_KEY):
            raise HTTPException(status_code=401)

    @app.exception_handler(401)
    async def unauthorized(request: Request, _exc: Exception) -> RedirectResponse:
        return RedirectResponse(f"/login?next={request.url.path}", HTTP_303_SEE_OTHER)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/") -> Any:
        if not auth.auth_required() or request.session.get(auth.SESSION_KEY):
            return RedirectResponse(next, HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": None}
        )

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request, password: str = Form(""), next: str = Form("/")
    ) -> Any:
        if auth.check_password(password):
            request.session[auth.SESSION_KEY] = True
            # Åbn kun interne stier, så et login-link ikke kan sende folk videre.
            target = next if next.startswith("/") and not next.startswith("//") else "/"
            return RedirectResponse(target, HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next, "error": "Forkert adgangskode."},
            status_code=401,
        )

    @app.get("/logout")
    def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", HTTP_303_SEE_OTHER)

    # -- fund --------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def finds(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        conn = queries.ro_conn(db_path())
        try:
            rows = queries.notifications(conn)
            active, _expired = queries.split_by_end(rows)
            reviews = queries.pending_reviews(conn)
        finally:
            conn.close()
        categories = sorted({r["category_key"] for r in active if r["category_key"]})
        prepared = rows_mod.prepare_all(active, db_path=db_path())
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
        conn = queries.ro_conn(db_path())
        try:
            rows = queries.notifications(conn)
            _active, gone = queries.split_by_end(rows)
        finally:
            conn.close()
        categories = sorted({r["category_key"] for r in gone if r["category_key"]})
        prepared = rows_mod.prepare_all(gone, db_path=db_path())
        return page(
            request, "finds.html",
            rows=prepared, groups=rows_mod.group_by_date(prepared),
            reviews=[], categories=categories,
            heading=f"Sluttet inden for {queries.EXPIRED_WINDOW_HOURS} timer",
            empty=f"Ingen lots er sluttet inden for de seneste "
                  f"{queries.EXPIRED_WINDOW_HOURS} timer.",
            show_ending=False, show_period=False,
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
        conn = queries.ro_conn(db_path())
        try:
            result = search_mod.search(conn, search_mod.SearchQuery(
                text=q, min_price=min_price, max_price=max_price,
                status=status, matched=matched, category=category,
                days_back=days_back, sort=sort, page=page_no or 1,
            ))
            stats = search_mod.archive_stats(conn)
            categories = search_mod.categories_seen(conn)
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
            rows=rows_mod.prepare_all(result.rows, seen_field="first_seen", db_path=db_path()),
            archive=stats, categories=categories, page_url=page_url,
            status_choices=search_mod.STATUS_CHOICES,
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

        return page(
            request, "interests.html",
            categories=categories, excludes=excludes, levels=LEVELS,
            message=message, error=error or read_error,
            test=test, trace=trace, explanation=explanation,
            editable=os.access(config_path() or "config/interests.yml", os.W_OK),
        )

    def _interests_redirect(message: str = "", error: str = "") -> RedirectResponse:
        from urllib.parse import urlencode
        params = {k: v for k, v in (("message", message), ("error", error)) if v}
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
            return _interests_redirect(message=note)
        except (EditError, OSError) as exc:
            return _interests_redirect(error=str(exc))

    @app.post("/interests/exclude")
    def interests_exclude(
        action: str = Form(...),
        keyword: str = Form(...),
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
            return _interests_redirect(message=note)
        except (EditError, OSError) as exc:
            return _interests_redirect(error=str(exc))

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
                message=f"Prisloftet for {category} er nu {kr(limit)}."
            )
        except (EditError, OSError) as exc:
            return _interests_redirect(error=str(exc))

    # -- assistent ---------------------------------------------------------

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(
        request: Request,
        q: str = Query("", max_length=chat_mod.MAX_QUESTION_LENGTH),
        _: None = Depends(require_login),
    ) -> HTMLResponse:
        answer = None
        if q.strip():
            conn = queries.ro_conn(db_path())
            try:
                answer = chat_mod.ask(conn, llm_client(), q)
            finally:
                conn.close()
        return page(
            request, "chat.html",
            question=q, answer=answer,
            rows=rows_mod.prepare_all(answer.rows, seen_field="first_seen", db_path=db_path()) if answer else [],
            has_key=llm_client() is not None,
        )

    # -- statistik ---------------------------------------------------------

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
        conn = queries.ro_conn(db_path())
        try:
            context = {
                "by_category": queries.category_breakdown(conn),
                "feedback": queries.feedback_breakdown(conn),
                "runs": queries.run_history(conn, limit=25),
                "risers": queries.price_risers(conn),
                "archive": search_mod.archive_stats(conn),
            }
        finally:
            conn.close()
        count, total = images_mod.usage(db_path())
        context["images"] = {
            "count": count,
            "mb": round(total / 1024 / 1024, 1),
        }
        return page(request, "stats.html", **context)

    @app.get("/export/feedback.csv")
    def export_feedback(_: None = Depends(require_login)) -> StreamingResponse:
        """Markeringer som CSV — træningsdata til AI-trinnet."""
        conn = queries.ro_conn(db_path())
        try:
            rows = queries.feedback_export(conn)
        finally:
            conn.close()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["lot_id", "kategori", "handling", "titel", "pris", "url", "tidspunkt"])
        for row in rows:
            writer.writerow([
                row["lot_id"], row["category_key"], row["action"], row["title"],
                row["last_total"] or "", row["url"] or "", row["created_at"],
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
    if not auth.auth_required():
        log.warning(
            "WEB_PASSWORD er ikke sat — dashboardet er åbent for alle på "
            "netværket, og det kan redigere interesseprofilen. Sæt WEB_PASSWORD."
        )
    log.info("Dashboard paa http://%s:%d", host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
