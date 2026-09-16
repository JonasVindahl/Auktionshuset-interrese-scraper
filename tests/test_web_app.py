"""HTTP-tests for hele dashboardet.

Kører mod en rigtig FastAPI-app med en rigtig database, så en fejl i en
skabelon eller en rute fanges her og ikke først i drift.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="dashboardet kræver FastAPI")

from fastapi.testclient import TestClient  # noqa: E402

from auction_hunter.web.app import create_app  # noqa: E402


@pytest.fixture()
def client(sample_db, sample_config, monkeypatch):
    """Dashboard uden adgangskode — auth testes for sig."""
    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FROM_ENV", raising=False)
    monkeypatch.delenv("CLASSIFIER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return TestClient(create_app())


@pytest.fixture()
def locked_client(sample_db, sample_config, monkeypatch):
    """Dashboard med adgangskode."""
    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.setenv("WEB_PASSWORD", "hemmelig123")
    monkeypatch.setenv("WEB_SECRET_KEY", "test-noegle-til-signering")
    monkeypatch.delenv("WEB_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FROM_ENV", raising=False)
    return TestClient(create_app(), follow_redirects=False)


# -- alle sider svarer -----------------------------------------------------

@pytest.mark.parametrize("path", [
    "/", "/expired", "/archive", "/interests", "/chat", "/stats",
    "/healthz", "/export/feedback.csv",
])
def test_siden_svarer(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:500]


def test_statiske_filer_serveres(client):
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_ukendt_sti_giver_404(client):
    assert client.get("/findes-ikke").status_code == 404


# -- fund og udløbet -------------------------------------------------------

def test_forsiden_viser_aktive_fund(client):
    body = client.get("/").text
    assert "Sennheiser" in body
    assert "Synology" in body


def test_forsiden_viser_ikke_udloebne(client):
    assert "Dell PowerEdge" not in client.get("/").text


def test_udloebet_viser_de_seneste_48_timer(client):
    body = client.get("/expired").text
    assert "Dell PowerEdge" in body      # sluttede for 5 timer siden
    assert "Quad 405" not in body        # sluttede for 70 timer siden


def test_forsiden_viser_gennemsynskoe(client):
    assert "Thorens" in client.get("/").text


def test_forsiden_viser_tid_tilbage(client):
    assert "Slutter om" in client.get("/").text


def test_forsiden_viser_billede(client):
    assert "auktionshuset.dk/i/1.jpg" in client.get("/").text


def test_alle_faner_er_i_menuen(client):
    body = client.get("/").text
    for href in ("/archive", "/interests", "/chat", "/stats"):
        assert f'href="{href}"' in body


# -- arkiv -----------------------------------------------------------------

def test_arkiv_finder_lot_der_aldrig_blev_fund(client):
    """Havemøblerne rammer ingen kategori, men skal kunne findes."""
    body = client.get("/archive", params={"q": "havemøbler"}).text
    assert "havemøbler" in body.lower()


def test_arkiv_filtrerer_til_ikke_fund(client):
    body = client.get(
        "/archive", params={"q": "harddiske", "matched": "kun_ikke_fund"}
    ).text
    assert "Synology" not in body


def test_arkiv_soeger_med_foldede_tegn(client):
    assert "Sennheiser" in client.get("/archive", params={"q": "hojttaler"}).text


def test_arkiv_viser_statistik(client):
    body = client.get("/archive").text
    assert "lots i arkivet" in body
    assert "uden for profilen" in body


def test_arkiv_taaler_ondsindet_soegning(client):
    """FTS5-syntaks i input må ikke give en fejl."""
    for probe in ['" OR "1"="1', "NEAR(a b)", "*", '"""', "a:b^c"]:
        assert client.get("/archive", params={"q": probe}).status_code == 200


def test_arkiv_paginering_bevarer_filtre(client):
    response = client.get("/archive", params={"q": "lot", "max_price": 5000})
    assert response.status_code == 200


# -- markeringer -----------------------------------------------------------

def test_feedback_gemmes_og_vises(client):
    response = client.post("/feedback", json={
        "lot_id": "a1", "category_key": "audio_hifi",
        "action": "bought", "title": "Sennheiser",
    })
    assert response.status_code == 200
    assert 'data-feedback="bought"' in client.get("/").text


def test_feedback_kan_fjernes(client):
    client.post("/feedback", json={
        "lot_id": "a1", "category_key": "audio_hifi", "action": "bought", "title": "x",
    })
    client.post("/feedback", json={
        "lot_id": "a1", "category_key": "audio_hifi", "action": "", "title": "x",
    })
    assert 'data-feedback="bought"' not in client.get("/").text


def test_feedback_afviser_ukendt_handling(client):
    response = client.post("/feedback", json={
        "lot_id": "a1", "category_key": "audio_hifi", "action": "drop table",
    })
    assert response.status_code == 400


def test_feedback_kraever_lot_id(client):
    assert client.post("/feedback", json={"action": "skip"}).status_code == 400


def test_csv_eksport_har_header(client):
    client.post("/feedback", json={
        "lot_id": "a1", "category_key": "audio_hifi", "action": "bought", "title": "x",
    })
    body = client.get("/export/feedback.csv").text
    assert body.startswith("lot_id,kategori,handling")
    assert "bought" in body


# -- interesser ------------------------------------------------------------

def test_interesser_viser_kategorier_og_noegleord(client):
    body = client.get("/interests").text
    assert "it_tech" in body
    assert "proxmark" in body
    assert "strong" in body


def test_interesser_viser_udelukkelser(client):
    assert "tastatur" in client.get("/interests").text


def test_tilfoej_noegleord_via_web(client, sample_config):
    response = client.post("/interests/keyword", data={
        "action": "add", "category": "it_tech",
        "level": "strong", "keyword": "webtestord",
    }, follow_redirects=False)
    assert response.status_code == 303

    from auction_hunter.web.yamledit import InterestsFile
    assert "webtestord" in InterestsFile(sample_config).keywords("it_tech", "strong")


def test_fjern_noegleord_via_web(client, sample_config):
    from auction_hunter.web.yamledit import InterestsFile

    client.post("/interests/keyword", data={
        "action": "add", "category": "it_tech",
        "level": "weak", "keyword": "midlertidig",
    })
    client.post("/interests/keyword", data={
        "action": "remove", "category": "it_tech",
        "level": "weak", "keyword": "midlertidig",
    })
    assert "midlertidig" not in InterestsFile(sample_config).keywords("it_tech", "weak")


def test_tilfoej_udelukkelse_via_web(client, sample_config):
    from auction_hunter.web.yamledit import InterestsFile

    client.post("/interests/exclude", data={"action": "add", "keyword": "webudeluk"})
    assert "webudeluk" in InterestsFile(sample_config).excludes()


def test_aendr_prisloft_via_web(client, sample_config):
    import yaml

    client.post("/interests/price", data={"category": "it_tech", "max_price": "9500"})
    with open(sample_config, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert data["categories"]["it_tech"]["max_price"] == 9500


def test_urimeligt_prisloft_afvises(client, sample_config):
    import yaml

    with open(sample_config, encoding="utf-8") as handle:
        before = yaml.safe_load(handle)["categories"]["it_tech"]["max_price"]

    client.post("/interests/price", data={"category": "it_tech", "max_price": "-5"})

    with open(sample_config, encoding="utf-8") as handle:
        after = yaml.safe_load(handle)["categories"]["it_tech"]["max_price"]
    assert after == before


def test_ugyldigt_niveau_afvises_af_ruten(client, sample_config):
    response = client.post("/interests/keyword", data={
        "action": "add", "category": "it_tech",
        "level": "ondsindet", "keyword": "x",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert "error" in response.headers["location"]


def test_redigering_bevarer_kommentarer(client, sample_config):
    def comments() -> int:
        with open(sample_config, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip().startswith("#"))

    before = comments()
    client.post("/interests/keyword", data={
        "action": "add", "category": "it_tech", "level": "strong", "keyword": "kommentartest",
    })
    client.post("/interests/exclude", data={"action": "add", "keyword": "ogsaatest"})
    client.post("/interests/price", data={"category": "it_tech", "max_price": "5555"})
    assert comments() == before


def test_test_en_titel(client):
    body = client.get("/interests", params={
        "test": "Sennheiser HD650 hovedtelefonforstærker",
    }).text
    assert "Matcher" in body


def test_test_en_udelukket_titel(client):
    body = client.get("/interests", params={"test": "Div. tastatur og mus"}).text
    assert "udelukket" in body.lower()


# -- assistent -------------------------------------------------------------

def test_chat_uden_noegle_svarer_alligevel(client):
    """Uden API-nøgle falder assistenten tilbage til nøgleordssøgning."""
    body = client.get("/chat", params={"q": "har der været nogen Sennheiser?"}).text
    assert "CLASSIFIER_API_KEY" in body
    assert "Sennheiser" in body


def test_chat_uden_spoergsmaal_viser_forslag(client):
    assert "Spørg om arkivet" in client.get("/chat").text


def test_chat_taaler_langt_input(client):
    assert client.get("/chat", params={"q": "a" * 5000}).status_code in (200, 422)


# -- statistik -------------------------------------------------------------

def test_statistik_viser_kategorier(client):
    body = client.get("/stats").text
    assert "it_tech" in body
    assert "Støjandel" in body


def test_statistik_viser_koersler(client):
    assert "Seneste kørsler" in client.get("/stats").text


def test_statistik_viser_stoejandel_efter_markering(client):
    client.post("/feedback", json={
        "lot_id": "a1", "category_key": "audio_hifi", "action": "skip", "title": "x",
    })
    assert "%" in client.get("/stats").text


# -- login -----------------------------------------------------------------

def test_uden_login_omdirigeres(locked_client):
    response = locked_client.get("/")
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_forkert_adgangskode_afvises(locked_client):
    response = locked_client.post("/login", data={"password": "forkert", "next": "/"})
    assert response.status_code == 401


def test_rigtig_adgangskode_giver_adgang(locked_client):
    response = locked_client.post(
        "/login", data={"password": "hemmelig123", "next": "/"}
    )
    assert response.status_code == 303
    assert locked_client.get("/").status_code == 200


def test_login_blokerer_ekstern_omdirigering(locked_client):
    """'next' må kun pege internt, ellers er login et videresendelsesværktøj."""
    response = locked_client.post("/login", data={
        "password": "hemmelig123", "next": "https://ondsindet.example",
    })
    assert response.headers["location"] == "/"


def test_login_blokerer_protokolrelativ_omdirigering(locked_client):
    response = locked_client.post("/login", data={
        "password": "hemmelig123", "next": "//ondsindet.example",
    })
    assert response.headers["location"] == "/"


def test_logout_lukker_adgangen(locked_client):
    locked_client.post("/login", data={"password": "hemmelig123", "next": "/"})
    assert locked_client.get("/").status_code == 200
    locked_client.get("/logout")
    assert locked_client.get("/").status_code == 303


def test_redigering_kraever_login(locked_client, sample_config):
    from auction_hunter.web.yamledit import InterestsFile

    locked_client.post("/interests/keyword", data={
        "action": "add", "category": "it_tech",
        "level": "strong", "keyword": "uautoriseret",
    })
    assert "uautoriseret" not in InterestsFile(sample_config).keywords("it_tech", "strong")


def test_feedback_kraever_login(locked_client):
    response = locked_client.post("/feedback", json={
        "lot_id": "a1", "category_key": "audio_hifi", "action": "bought",
    })
    assert response.status_code in (303, 401)


def test_uden_adgangskode_er_alt_aabent(client):
    """Bagudkompatibelt: uden WEB_PASSWORD kræves intet login."""
    assert client.get("/").status_code == 200


# -- robusthed -------------------------------------------------------------

def test_tom_database_giver_stadig_sider(tmp_path, sample_config, monkeypatch):
    from auction_hunter.storage import Store

    path = tmp_path / "tom.db"
    with Store(path):
        pass

    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    empty = TestClient(create_app())

    for path_name in ("/", "/expired", "/archive", "/stats"):
        assert empty.get(path_name).status_code == 200
    assert "Ingen aktive fund" in empty.get("/").text


def test_titel_med_html_escapes(tmp_path, sample_config, monkeypatch, lot_factory):
    from auction_hunter.storage import Store

    path = tmp_path / "xss.db"
    with Store(path) as store:
        run_id = store.start_run()
        store.record_lots([lot_factory("x1", "<script>alert(1)</script>")])
        store.mark_notified("x1", "hifi", 100)
        store.finish_run(run_id)

    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)

    body = TestClient(create_app()).get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_healthz_paa_manglende_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "findes-ikke.db"))
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    assert TestClient(create_app()).get("/healthz").status_code == 503
