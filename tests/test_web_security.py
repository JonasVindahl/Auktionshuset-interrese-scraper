"""Sikkerhedstests for dashboardet.

Hver test her vogter en fejl der er rettet, så den ikke kommer tilbage. De er
skrevet som angreb mod en kørende app, ikke som enhedstests af hjælpefunktioner.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("fastapi", reason="dashboardet kræver FastAPI")

from fastapi.testclient import TestClient  # noqa: E402

from auction_hunter.web.app import create_app  # noqa: E402


@pytest.fixture()
def client(sample_db, sample_config, monkeypatch):
    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FROM_ENV", raising=False)
    return TestClient(create_app())


# -- injektion i skabeloner -------------------------------------------------

def test_noegleord_kan_ikke_bryde_ud_af_javascript(client):
    """Regression: et nøgleord med et citationstegn kørte JS via onsubmit.

    Autoescape er slået til, men browseren HTML-dekoder en attributværdi før
    JavaScript-parsing, så et &#39; blev til et rigtigt citationstegn og
    afsluttede strengen i confirm(...).
    """
    payload = "foo');alert(1)//"
    client.post("/interests/keyword", data={
        "action": "add", "category": "it_tech", "level": "strong", "keyword": payload,
    })
    body = client.get("/interests").text

    assert "onsubmit" not in body, "inline JS er tilbage i skabelonen"
    assert "foo');alert" not in body, "citationstegnet slap rå igennem"
    assert "foo&#39;);alert(1)//" in body, "nøgleordet vises ikke escaped"


def test_udelukkelse_kan_ikke_bryde_ud_af_javascript(client):
    payload = "bar');alert(2)//"
    client.post("/interests/exclude", data={"action": "add", "keyword": payload})
    body = client.get("/interests").text

    assert "onsubmit" not in body
    assert "bar');alert" not in body
    assert "bar&#39;);alert(2)//" in body


# -- omdirigering -----------------------------------------------------------

def test_login_omdirigerer_kun_internt(client):
    """Regression: GET /login?next=https://... sendte folk videre eksternt."""
    for evil in (
        "https://ondsindet.example/x",
        "//ondsindet.example",
        "javascript:alert(1)",
        "http://localhost:8080/",
    ):
        response = client.get("/login", params={"next": evil}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/", evil


def test_login_omdirigerer_internt_naar_stien_er_lokal(client):
    response = client.get("/login", params={"next": "/archive"}, follow_redirects=False)
    assert response.headers["location"] == "/archive"


# -- CSV --------------------------------------------------------------------

def test_csv_eksport_neutraliserer_formler(client):
    """Excel og Sheets evaluerer celler der begynder med = + - @."""
    client.post("/feedback", json={
        "lot_id": "a1", "category_key": "audio_hifi",
        "action": "bought", "title": "=cmd|' /C calc'!A0",
    })
    body = client.get("/export/feedback.csv").text
    assert "'=cmd" in body, "formlen blev ikke neutraliseret"
    assert "\n=cmd" not in body


# -- adgangskode ------------------------------------------------------------

def test_ulaeselig_adgangskode_lukker_dashboardet(
    sample_db, sample_config, monkeypatch, tmp_path
):
    """Regression: en forkert WEB_PASSWORD_FILE åbnede hele dashboardet.

    Forskellen mellem "ingen adgangskode er sat" og "adgangskoden kunne ikke
    læses" er hele pointen. Den første er et valg, den anden en fejl.
    """
    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.setenv("WEB_PASSWORD_FILE", str(tmp_path / "findes-ikke"))
    monkeypatch.delenv("WEB_PASSWORD_FROM_ENV", raising=False)

    locked = TestClient(create_app(), follow_redirects=False)
    for route in ("/", "/archive", "/interests", "/stats", "/chat", "/export/feedback.csv"):
        assert locked.get(route).status_code == 503, route


# -- headere ----------------------------------------------------------------

def test_sikkerhedsheadere(client):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    policy = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy
    assert "script-src 'self'" in policy
    assert response.headers["cache-control"] == "no-store"


def test_csp_tillader_ikke_inline_script(client):
    """Skabelonerne må ikke bruge inline event-handlere, ellers dør CSP'en."""
    for route in ("/", "/archive", "/interests", "/stats", "/chat"):
        body = client.get(route).text
        assert " onerror=" not in body, route
        assert " onclick=" not in body, route
        assert " onsubmit=" not in body, route


def test_openapi_er_slukket(client):
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


# -- urimelige parametre ----------------------------------------------------

@pytest.mark.parametrize("params", [
    {"days_back": "740000"},
    {"days_back": "99999999999999999999"},
    {"min_price": "99999999999999999999"},
    {"max_price": "99999999999999999999"},
    {"page": "999999999"},
    {"page": "-5"},
])
def test_urimelige_talfelter_giver_ikke_500(client, params):
    """Regression: et stort nok tal fik sqlite3 og datetime til at kaste."""
    response = client.get("/archive", params=params)
    assert response.status_code == 200, (params, response.status_code)


# -- database uden skema ----------------------------------------------------

def test_database_uden_tabeller_giver_tomme_sider(tmp_path, sample_config, monkeypatch):
    """Regression: en eksisterende, tom .db-fil gav 500 på alle sider."""
    path = tmp_path / "umigreret.db"
    sqlite3.connect(path).close()

    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)

    empty = TestClient(create_app())
    for route in ("/", "/expired", "/archive", "/stats", "/chat"):
        assert empty.get(route).status_code == 200, route


def test_manglende_database_giver_tomme_sider(tmp_path, sample_config, monkeypatch):
    """At starte dashboardet før agenten har kørt må ikke give en 500."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "findes-slet-ikke.db"))
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)

    empty = TestClient(create_app())
    for route in ("/", "/archive", "/stats"):
        assert empty.get(route).status_code == 200, route

    # Healthz skal stadig melde fra, så overvågningen kan se forskel.
    assert empty.get("/healthz").status_code == 503


def test_metrics_token_med_ikke_ascii_giver_401_ikke_500(sample_db, monkeypatch):
    """Et forkert token skal afvises, ogsaa naar det ikke er ASCII.

    hmac.compare_digest afviser str med ikke-ASCII, saa ?token=æøå kastede en
    TypeError og blev til en 500 paa et endpunkt der er aabent som standard.
    """
    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("METRICS_TOKEN", "hemmeligt-token")
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    client = TestClient(create_app(), raise_server_exceptions=False)

    assert client.get("/metrics", params={"token": "æøå"}).status_code == 401
    assert client.get("/metrics", params={"token": "forkert"}).status_code == 401
    assert client.get("/metrics", params={"token": "hemmeligt-token"}).status_code == 200
