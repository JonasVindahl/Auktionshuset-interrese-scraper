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
    "/", "/expired", "/mine", "/archive", "/interests", "/chat", "/stats",
    "/drift", "/healthz", "/export/feedback.csv",
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


# Browseren sender hvert felt i formularen med, også de tomme. Uden at tomme
# strenge oversættes til None svarer FastAPI 422 på en helt almindelig søgning.
BROWSER_FORM = {
    "q": "nas", "min_price": "", "max_price": "", "status": "alle",
    "matched": "alle", "category": "", "days_back": "", "sort": "relevans",
}


def test_arkiv_taaler_tom_formular(client):
    """Regression: en søgning uden prisfilter gav 422."""
    response = client.get("/archive", params=BROWSER_FORM)
    assert response.status_code == 200, response.text[:400]


def test_arkiv_taaler_helt_tom_formular(client):
    """Samme, men også uden søgeord."""
    response = client.get("/archive", params={**BROWSER_FORM, "q": ""})
    assert response.status_code == 200


@pytest.mark.parametrize("field", ["min_price", "max_price", "days_back"])
def test_arkiv_taaler_enkelt_tomt_talfelt(client, field):
    params = {**BROWSER_FORM, field: ""}
    assert client.get("/archive", params=params).status_code == 200


def test_arkiv_taaler_tom_side(client):
    assert client.get("/archive", params={**BROWSER_FORM, "page": ""}).status_code == 200


def test_arkiv_bruger_udfyldte_talfelter(client):
    """Tomme felter ignoreres, men udfyldte skal stadig virke."""
    response = client.get("/archive", params={
        **BROWSER_FORM, "q": "", "min_price": "1000", "max_price": "",
    })
    assert response.status_code == 200
    assert "Havemøbler" not in response.text     # koster 280 kr


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


def test_tomt_prisloft_giver_besked_ikke_serverfejl(client, sample_config):
    """Et tomt felt er brugerfejl, ikke 422."""
    response = client.post(
        "/interests/price", data={"category": "it_tech", "max_price": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error" in response.headers["location"]


def test_prisloft_der_ikke_er_et_tal(client, sample_config):
    response = client.post(
        "/interests/price", data={"category": "it_tech", "max_price": "mange"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error" in response.headers["location"]


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
    response = client.post(
        "/chat", data={"q": "har der været nogen Sennheiser?", "c": ""}
    )
    assert response.status_code == 200
    assert "CLASSIFIER_API_KEY" in response.text
    assert "Sennheiser" in response.text


def test_chat_husker_samtalen(client, monkeypatch):
    """Anden tur skal bære det forrige filter videre til modellen."""
    import auction_hunter.web.app as appmod

    seen: list[str] = []

    class Sequence:
        def complete(self, *, system: str, user: str, max_tokens: int = 120) -> str:
            seen.append(user)
            return ('{"kategori": "it_tech"}' if "Forrige filter" not in user
                    else '{"kategori": "it_tech"}')

    monkeypatch.setattr(appmod, "llm_client", lambda: Sequence())
    client.post("/chat", data={"q": "hvad er der af it?", "c": ""})

    monkeypatch.setattr(appmod, "llm_client", lambda: Sequence())
    # Anden tur fortsætter den seneste samtale, og skal se det forrige filter.
    response = client.post("/chat", data={"q": "og hvad med dem under 1000?", "c": ""})
    assert response.status_code == 200
    assert any("Forrige filter" in user for user in seen)


def test_ny_samtale_starter_forfra(client, monkeypatch):
    import auction_hunter.web.app as appmod

    class Sequence:
        def complete(self, *, system: str, user: str, max_tokens: int = 120) -> str:
            return '{"kategori": "it_tech"}'

    monkeypatch.setattr(appmod, "llm_client", lambda: Sequence())
    client.post("/chat", data={"q": "foerste samtale", "c": ""})
    response = client.post("/chat/new")
    assert response.status_code == 200
    # Den nye samtale er tom, saa beskeden fra den foerste er ikke i traaden.
    # Titlen maa gerne staa i historik-listen.
    assert "<p>foerste samtale</p>" not in response.text
    assert "Spørg arkivet" in response.text


def test_chat_uden_spoergsmaal_viser_forslag(client):
    assert "Spørg om arkivet" in client.get("/chat").text


def test_chat_taaler_langt_input(client):
    # Spoergsmaalet klippes til MAX_QUESTION_LENGTH i ruten.
    assert client.post("/chat", data={"q": "a" * 5000, "c": ""}).status_code == 200


def test_chat_ignorerer_ugyldigt_samtale_id(client):
    for value in ("abc", "999999", "-3", ""):
        assert client.get("/chat", params={"c": value}).status_code == 200


def test_chat_tomt_spoergsmaal_laver_ingen_besked(client):
    response = client.post("/chat", data={"q": "   ", "c": ""})
    assert response.status_code == 200
    assert "turn-user" not in response.text


def test_samtalefilen_ligger_ved_siden_af_agentens(client, sample_db):
    from pathlib import Path

    client.post("/chat", data={"q": "hej", "c": ""})
    assert (Path(sample_db).parent / "conversations.db").exists()


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
    # Log ud er en POST, saa den ikke kan rammes af et <img src> fra en anden side.
    locked_client.post("/logout")
    assert locked_client.get("/").status_code == 303


def test_for_mange_loginfoersoeg_blokeres(locked_client):
    for _ in range(5):
        locked_client.post("/login", data={"password": "forkert", "next": "/"})
    response = locked_client.post(
        "/login", data={"password": "hemmelig123", "next": "/"}
    )
    assert response.status_code == 429


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


# -- cachede billeder ------------------------------------------------------

def _cache_image(db_path: str, lot_id: str) -> bytes:
    """Læg et rigtigt billede i cachen, som en kørsel ville have gjort."""
    from tests.test_images import PNG  # noqa: PLC0415

    from auction_hunter import images

    path = images.cache_path(db_path, lot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    return PNG


def test_cachet_billede_udleveres(client, sample_db):
    data = _cache_image(sample_db, "a1")
    response = client.get("/image/a1")
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/png"


def test_ucachet_billede_giver_404(client):
    """404 er meningen: skabelonen falder tilbage til den levende adresse."""
    assert client.get("/image/findes-ikke").status_code == 404


def test_kort_peger_paa_cachet_billede(client, sample_db):
    """Når billedet er hentet, skal siden bruge det og ikke auktionshusets."""
    _cache_image(sample_db, "a1")
    body = client.get("/").text
    assert 'src="/image/a1"' in body
    assert "auktionshuset.dk/i/1.jpg" not in body


def test_kort_bruger_levende_adresse_uden_cache(client):
    assert "auktionshuset.dk/i/1.jpg" in client.get("/").text


def test_udloebet_lot_viser_stadig_sit_billede(client, sample_db):
    """Hele pointen: originalen er død, men vores kopi lever."""
    _cache_image(sample_db, "a3")        # Dell PowerEdge, sluttede for 5 t siden
    body = client.get("/expired").text
    assert 'src="/image/a3"' in body


def test_billede_kraever_login(locked_client, sample_db):
    _cache_image(sample_db, "a1")
    assert locked_client.get("/image/a1").status_code in (303, 401)


def test_billedrute_taaler_ondsindet_lot_id(client):
    """lot_id kommer fra auktionshusets HTML og må ikke kunne læse filer."""
    for evil in ["..%2F..%2F..%2Fetc%2Fpasswd", "....//....//etc/passwd"]:
        assert client.get(f"/image/{evil}").status_code in (404, 400)


def test_chat_viser_vurderingspanel(client, monkeypatch):
    """Når spørgsmålet handler om hvad noget er værd, vises arkivets egne tal."""
    import auction_hunter.web.app as appmod

    class Sequence:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, max_tokens=120):
            self.calls += 1
            if self.calls == 1:
                return '{"kategori": "it_tech"}'
            return ('{"valgte": [1], "svar": "Her er hvad den er værd.", '
                    '"sammenlign": 1}')

    monkeypatch.setattr(appmod, "llm_client", lambda: Sequence())
    response = client.post("/chat", data={"q": "hvad er den vaerd", "c": ""})
    assert response.status_code == 200
    assert "Hvad samme slags er gået for" in response.text


# -- version og klarhed ----------------------------------------------------

def test_healthz_rapporterer_version(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["version"]


def test_readyz_kraever_database_og_konfiguration(client):
    body = client.get("/readyz").json()
    assert body["ok"] is True
    assert body["kategorier"] >= 1
    assert body["regioner"]


def test_drift_viser_version_og_regioner(client):
    body = client.get("/drift").text
    assert "Version" in body
    assert "Python" in body
    assert "Regioner" in body


# -- profiler i webben -----------------------------------------------------

PROFIL_CONFIG = """\
source:
  region_ids: all
regions:
  Sjaelland: sjae
  Fyn: fyn
budget:
  max_price: 5000
  soft_over_budget_factor: 2.5
categories:
  it_tech:
    label: IT
    strong: [server]
  audio_hifi:
    label: Audio
    strong: [forstaerker]
profiles:
  it:
    label: IT og netvaerk
    categories: [it_tech]
  lyd:
    label: Lyd
    categories: [audio_hifi]
    webhook_env: DISCORD_WEBHOOK_LYD
"""


@pytest.fixture()
def profile_client(sample_db, tmp_path, monkeypatch):
    """Dashboard med to profiler i konfigurationen."""
    config = tmp_path / "profiler.yml"
    config.write_text(PROFIL_CONFIG, encoding="utf-8")
    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", str(config))
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FROM_ENV", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_LYD", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_LYD_FILE", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_LYD_FROM_ENV", raising=False)
    return TestClient(create_app())


def _mark_profile(db: str, lot_id: str, category: str, profile: str, cost: int = 100):
    from auction_hunter.storage import Store

    with Store(db) as store:
        store.mark_notified(lot_id, category, cost, profile)


def test_fund_kan_filtreres_paa_profil(profile_client, sample_db):
    _mark_profile(sample_db, "a1", "audio_hifi", "lyd")

    alle = profile_client.get("/").text
    assert 'data-profile="standard"' in alle
    assert 'data-lot="a2"' in alle

    kun_lyd = profile_client.get("/?profile=lyd").text
    assert 'data-lot="a1"' in kun_lyd
    assert 'data-profile="lyd"' in kun_lyd
    assert 'data-lot="a2"' not in kun_lyd


def test_fund_viser_profilnavn_naar_den_ikke_er_standard(profile_client, sample_db):
    _mark_profile(sample_db, "a1", "audio_hifi", "lyd")
    body = profile_client.get("/?profile=lyd").text
    assert "Lyd" in body


def test_fund_viser_profilchips(profile_client):
    body = profile_client.get("/").text
    assert "IT og netvaerk" in body
    assert "profile=lyd" in body


def test_drift_viser_profiler_med_webhookstatus(profile_client):
    body = profile_client.get("/drift").text
    assert "Profiler" in body
    assert "DISCORD_WEBHOOK_LYD" in body
    assert "ikke sat" in body


# -- prod-sikkerhed --------------------------------------------------------

def test_post_fra_fremmed_oprindelse_afvises(client):
    response = client.post("/logout", headers={"Origin": "https://ondsindet.example"})
    assert response.status_code == 403


def test_post_fra_samme_oprindelse_tillades(client):
    response = client.post("/logout", headers={"Origin": "http://testserver"})
    assert response.status_code != 403


def test_post_uden_oprindelse_tillades(client):
    """curl og testklienten sender ingen Origin; kun browsere kan lave CSRF."""
    assert client.post("/logout").status_code != 403


def test_cookie_secure_udledes_af_web_base_url(monkeypatch):
    from auction_hunter.web import app as appmod

    monkeypatch.delenv("WEB_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("WEB_BASE_URL", "https://hunter.example")
    assert appmod.cookie_secure() is True

    monkeypatch.setenv("WEB_BASE_URL", "http://192.168.1.10:8080")
    assert appmod.cookie_secure() is False


def test_cookie_secure_kan_tvinges(monkeypatch):
    from auction_hunter.web import app as appmod

    monkeypatch.setenv("WEB_BASE_URL", "https://hunter.example")
    monkeypatch.setenv("WEB_COOKIE_SECURE", "0")
    assert appmod.cookie_secure() is False

    monkeypatch.setenv("WEB_COOKIE_SECURE", "1")
    assert appmod.cookie_secure() is True


def test_allowed_hosts_er_tom_uden_miljoevariabel(monkeypatch):
    from auction_hunter.web import app as appmod

    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    assert appmod.allowed_hosts() == []
    monkeypatch.setenv("ALLOWED_HOSTS", "hunter.example, 10.0.0.5")
    assert appmod.allowed_hosts() == ["hunter.example", "10.0.0.5"]


def test_kun_tilladte_hosts_accepteres(sample_db, sample_config, monkeypatch):
    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver")
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FROM_ENV", raising=False)
    guarded = TestClient(create_app())

    assert guarded.get("/healthz").status_code == 200
    response = guarded.get("/healthz", headers={"Host": "ondsindet.example"})
    assert response.status_code == 400


# -- fejl-sider ------------------------------------------------------------

def test_ukendt_side_giver_en_styled_404(client):
    response = client.get("/findes-ikke")
    assert response.status_code == 404
    assert "404" in response.text
    assert "Tilbage til fund" in response.text


def test_uventet_fejl_giver_en_styled_500(sample_db, sample_config, monkeypatch):
    from auction_hunter.web import app as appmod

    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FILE", raising=False)
    app = appmod.create_app()

    def boom(*_args, **_kwargs):
        raise RuntimeError("planlagt fejl i testen")

    monkeypatch.setattr(appmod.queries, "notifications", boom)
    # raise_server_exceptions=False, saa vi ser den side brugeren ville faa.
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")
    assert response.status_code == 500
    assert "500" in response.text
    assert "Tilbage til fund" in response.text


# -- metrics ---------------------------------------------------------------

def test_metrics_er_prometheus_tekst(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "hunter_up 1" in body
    assert 'hunter_build_info{version="' in body
    assert "# TYPE hunter_table_rows gauge" in body


def test_metrics_uden_token_naar_intet_er_sat(client):
    assert client.get("/metrics").status_code == 200


def test_metrics_kraever_token_naar_sat(sample_db, sample_config, monkeypatch):
    from auction_hunter.web import app as appmod

    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.setenv("METRICS_TOKEN", "hemmeligt-token")
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    guard = TestClient(appmod.create_app())

    assert guard.get("/metrics").status_code == 401
    assert guard.get("/metrics?token=forkert").status_code == 401

    ok = guard.get("/metrics", headers={"Authorization": "Bearer hemmeligt-token"})
    assert ok.status_code == 200
    assert guard.get("/metrics?token=hemmeligt-token").status_code == 200


def test_metrics_lækker_ikke_hemmeligheder(sample_db, sample_config, monkeypatch):
    from auction_hunter.web import app as appmod

    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/hemmelig")
    monkeypatch.setenv("CLASSIFIER_API_KEY", "sk-hemmelig-noegle")
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    body = TestClient(appmod.create_app()).get("/metrics").text
    assert "hemmelig" not in body
    assert "sk-" not in body



# -- arkivets auktionsfilter -----------------------------------------------

def test_archive_auction_filter_narrows_result(client):
    """Filtret skal skære de andre auktioner væk, ikke bare svare 200.

    SearchQuery, search(), filter_chips og <select name="auction"> havde alle
    feltet, men ruten læste det aldrig, og FastAPI ignorerer ukendte
    query-parametre i stilhed. Testen asserterer derfor at to auktioner bliver
    til én, ikke at siden kan vises.
    """
    # Uden filter er begge auktioner med i resultatet.
    alle = client.get("/archive").text
    assert "PowerEdge" in alle and "Synology" in alle

    # Med filter maa kun lots fra Koege staa tilbage. Auktionsnavnet i sig selv
    # duer ikke som assertion: det staar ogsaa i filterets egen <option>-liste.
    kun = client.get("/archive", params={"auction": "Auktion Køge"}).text
    assert "PowerEdge" in kun, "lot'et fra den valgte auktion mangler"
    assert "Synology" not in kun, "lots fra andre auktioner skulle vaere filtreret fra"
    assert "Sennheiser" not in kun


def test_archive_auction_filter_survives_pagination_links(client):
    """Sidelinks skal bære filtret videre, ellers falder det af på side 2."""
    body = client.get(
        "/archive", params={"q": "e", "auction": "Auktion Køge", "page": 1}
    ).text
    assert "Auktion+K%C3%B8ge" in body or "auction=Auktion" in body


# -- skabeloner maa ikke ramme dict-metoder ved et uheld --------------------

# Jinja proever attributten foer noeglen, saa et opslag som 'x.items' rammer
# dict.items og ikke noeglen "items", hvis x er en dict. Det er tavst: man
# faar en bundet metode i stedet for en vaerdi, og fejlen dukker foerst op i
# den loekke eller det filter der skal bruge den.
DICT_METODER = frozenset({
    "items", "keys", "values", "get", "pop", "popitem", "clear", "copy",
    "update", "setdefault", "fromkeys",
})


def _kollisioner_i(tekst: str) -> list[tuple[int, str]]:
    """(linjenummer, udtryk) for attribut-opslag der skygger for en dict-metode.

    Et rigtigt metodekald som 'labels.get(x)' eller 'secrets.items()' er i
    orden — der er parentesen med vilje. Det er opslaget uden parentes der er
    tvetydigt.
    """
    import re

    fundet = []
    for n, linje in enumerate(tekst.splitlines(), 1):
        if "{{" not in linje and "{%" not in linje:
            continue
        for objekt, attribut in re.findall(r"(\w+)\.(\w+)", linje):
            if attribut in DICT_METODER and f"{objekt}.{attribut}(" not in linje:
                fundet.append((n, f"{objekt}.{attribut}"))
    return fundet


def test_ingen_skabelon_slaar_op_paa_et_dict_metodenavn():
    """En vagt mod den fejl der gav 500 paa /chat.

    'comps.items' virkede paa Comparables-dataklassen og braekkede i samme
    oejeblik det samme panel blev renderet fra en gemt samtale, hvor objektet
    er en almindelig dict. Skriv 'x["items"]' hvis noeglen virkelig hedder
    det, eller giv feltet et navn der ikke skygger for en dict-metode.
    """
    from pathlib import Path

    from auction_hunter.web import app as app_mod

    problemer = []
    for fil in sorted((Path(app_mod.HERE) / "templates").glob("*.html")):
        for linje, udtryk in _kollisioner_i(fil.read_text(encoding="utf-8")):
            problemer.append(f"{fil.name}:{linje}  {udtryk}")

    assert not problemer, (
        "Skabeloner slår op på et navn der også er en dict-metode:\n  "
        + "\n  ".join(problemer)
    )


# -- auktionsinfo i arkivet ------------------------------------------------

def _arkiv_client(tmp_path, sample_config, monkeypatch, lots):
    from dataclasses import replace

    from tests.conftest import make_lot

    from auction_hunter.storage import Store

    db = tmp_path / "arkiv.db"
    with Store(db) as store:
        store.record_lots(
            [replace(make_lot(lid, title), **felt) for lid, title, felt in lots],
            {lid: 150 for lid, _, _ in lots},
        )
        store.conn.commit()

    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("CONFIG_PATH", sample_config)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("WEB_PASSWORD_FROM_ENV", raising=False)
    return TestClient(create_app())


def test_arkiv_viser_og_filtrerer_levering(tmp_path, sample_config, monkeypatch):
    client = _arkiv_client(tmp_path, sample_config, monkeypatch, [
        ("ship", "Switch der kan sendes",
         {"region": "Fyn", "auction_type": "Konkursauktion", "shipping": True}),
        ("pick", "Switch der kun afhentes", {"region": "Sjælland", "shipping": False}),
    ])

    alle = client.get("/archive").text
    assert "kan sendes" in alle
    assert "Switch der kan sendes" in alle

    kun_forsendelse = client.get("/archive?shipping=1").text
    assert "Switch der kan sendes" in kun_forsendelse
    assert "Switch der kun afhentes" not in kun_forsendelse

    fyn = client.get("/archive?region=Fyn").text
    assert "Switch der kan sendes" in fyn
    assert "Switch der kun afhentes" not in fyn
