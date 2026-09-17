# Ændringslog

Formatet følger [Keep a Changelog](https://keepachangelog.com/da/1.1.0/), og
versionerne er [semantiske](https://semver.org/lang/da/).

## [Unreleased]

### Rettet

- **`Profile.classifier_profile` blev aldrig brugt.** Feltet blev læst fra
  YAML ind i dataklassen, men alle profilers fund blev bedømt med den globale
  prompt. Alle profilens øvrige felter blev respekteret, så det her var det
  eneste der ikke virkede, og det gjorde det tavst. Den effektive prompt
  indgår nu også i cache-nøglen, så to profiler med hver sin smag får hver
  sit svar på samme lot.
- **Dashboardets AI-forbrug var ikke loftet**, selvom agentens har været det
  hele tiden via `classifier.max_per_run`. Ét chat-spørgsmål er to kald og
  forslagsknappen er ét kald pr. klik, så en genindlæsning brugte nøglen uden
  grænse. Loftet er 60 kald pr. time, sat med `WEB_AI_MAX_PER_HOUR`, og
  forbruget står på `/drift`. Rammes det, opfører siderne sig som uden en
  nøgle i stedet for at fejle.

- **Auktionslisten blev ikke pagineret.** `fetch_auctions` hentede kun side 1,
  så agenten så de første ~24 auktioner og meldte alligevel succes. Fejlen var
  latent så længe standarden var Sjælland alene, og blev aktiv med
  `region_ids: all`. Blindheds-tjekket kunne ikke fange den, fordi
  lot-antallet stadig var stort.
- **Arkivets auktionsfilter virkede ikke.** `SearchQuery`, `search()`,
  filter-chips og formularens `<select>` havde alle feltet, men `/archive`
  læste aldrig parameteren, og FastAPI ignorerer ukendte query-parametre i
  stilhed. Samme felt manglede i assistentens arkiv-link.
- **Nøgleord matchede ikke som forled i sammensatte ord.** `netværk` fandt
  ikke `netværksswitch`, `højttaler` ikke `højttalerkabinet`. Kun efterled
  virkede, selvom docstringen lovede begge dele. Forled har nu sin egen
  længdegrænse på 8 tegn, målt ud fra `server`/`kaffeservering` og
  `batteri`/`batteridrevet`.
- **AI-cachen dækkede ikke prompten.** `classifier.profile` indgik ikke i
  cache-nøglen, så en rettet profil genbrugte gamle domme i op til 180 dage.
- **`max_per_run` talte cache-opslag som kald**, så cachede fund kunne bruge
  hele budgettet uden at der blev ringet.
- **`/metrics?token=<ikke-ascii>` gav 500** i stedet for 401.
- **To samtidige redigeringer af `interests.yml` tabte den ene** uden spor.
- **`restore` kunne køre mens agenten kørte**, hvorved gendannelsen gik tabt.
  Håndhæves nu med et livstegn, som kan tilsidesættes med `--force`.
- **`tools/evaluate.py` kunne ikke læse det `export` producerer**, så den
  dokumenterede måling virkede ikke ad nogen af de tre beskrevne veje.
- Dockerfilens OCI-label sagde stadig `proprietary` efter skiftet til MIT.
- `backups/` var ikke i `.gitignore`, så `make backup` lagde hele arkivet i
  arbejdstræet.
- En frisk database blev oprettet for straks at blive migreret tre gange.

### Tilføjet

- Facitlisten kan bære et valgfrit `via`-felt: navnet på det nøgleord der skal
  bære en case. Uden det er en case opfyldt så snart noget rammer, og den kan
  gå igennem ad en anden vej end den den skulle dække. Listens seks
  netværkslinjer læste som forleds-tilfælde, men blev alle båret af
  efterleddet `switch`; de er nu tagget med `via: switch`.
- Hver kørsel gemmer hvor mange HTTP-kald den kostede, og `/drift` viser det.
  Projektet har beskrevet sig selv som "ét scrape hvert 15. minut"; det er
  intervallet, ikke antallet af kald, og nu kan forskellen efterprøves.
- `SCRAPER_USER_AGENT` kan sætte User-Agent uden en kodeændring.
- `LICENSE`: MIT.
- CI kører nu på GitHub: tests på Python 3.11 og 3.13, ruff og et Docker-build.
  Badge i README.

### Rettet

- CI's første kørsel fangede at `httpx2` manglede i testopsætningen. Lokalt lå
  `httpx` i forvejen, så fejlen var skjult indtil testene kørte på en ren
  maskine.

### Ændret

- `auction_hunter backup` tager nu hele hukommelsen med: agentens database,
  samtalernes database og billedcachen, i én `tar.gz`. Gendannelse sikrer hver
  del der overskrives og kan rulles tilbage.

## [1.1.0] - 2026-09-17

Produktionsfundament og dynamiske interesseprofiler. Funktionerne er de samme,
men agenten kan nu dække hele landet, køre flere profiler side om side, og
driften er dækket af backup, metrics og fejl-sider.

### Tilføjet

- **Regioner:** `region_ids: all` følger alle landsdele fra `regions`-mappingen.
  Etiketten i beskederne udledes automatisk ("Hele Danmark"), så varslerne ikke
  påstår "Sjælland" mens agenten følger hele landet. Kan overstyres med
  `REGION_LABEL`.
- **Profiler:** et navngivet interessesæt med egne kategorier, eget prisloft,
  egne udelukkelser og egen Discord-webhook. Uden `profiles` i
  `interests.yml` er der én implicit standardprofil, og alt kører som før.
- **Backup og gendannelse:** `auction_hunter backup` og `restore`. Bruger
  SQLites `VACUUM INTO`, tjekker integriteten, og sletter et ugyldigt backup.
- **Metrics:** `/metrics` i Prometheus-format med aggregerede tal. Kan kræve
  bearer-token via `METRICS_TOKEN`.
- **Klarhed:** `/readyz` kræver både database og læsbar konfiguration.
- **Version synligt:** `/healthz`, `/readyz`, sidefoden og `/drift`, plus
  OCI-labels på imaget.
- **Fejl-sider:** stylede 404 og 500.
- **Makefile** med de hyppige kommandoer, og `pyproject.toml` med metadata,
  console-script og ruff-config.
- **CI:** `ci/ci.yml` kører tests på Python 3.11 og 3.13, ruff og et
  Docker-build. Se afsnit 9 i `DEPLOYMENT.md` for aktivering.

### Ændret

- Notifikationer dedupes pr. (lot, kategori, profil) i stedet for pr.
  (lot, kategori). Eksisterende rækker hører til standardprofilen.
- Pris- og sidste-chance-varsler dedupes pr. lot, så et lot der matcher to
  profiler ikke giver to ens beskeder.
- Fund- og udløbssiderne kan filtreres på profil server-side.
- Ukendte kategorinavne i en profil stopper opstarten i stedet for at matche
  stille på ingenting.

### Sikkerhed

- Login-cookien kan kræve HTTPS (`WEB_COOKIE_SECURE` eller `WEB_BASE_URL`).
- `ALLOWED_HOSTS` afviser fremmede `Host`-headere.
- Proxy-headere læses, så scheme og klient-IP er rigtige bag en reverse proxy.
- Usikre metoder afvises, hvis `Origin`/`Referer` peger på en anden vært.
- Base-imaget er pinnet til et digest, og compose har resource limits og en
  konfigurerbar port-binding.

### Rettet

- Blindhedsadvarslen skrev hardkodet "Sjælland" uanset hvilke regioner der blev
  fulgt.
- `auction_hunter backup` fejlede tidligere ikke; der fandtes ingen kommando.
- `/metrics` ville have sendt en 401 gennem login-omdirigeringen og dermed
  lavet en uendelig redirect-løkke. Den svarer nu direkte.

## [1.0.0] - 2026-09-16

Første version: scraper, nøgleordsmatchning, SQLite-hukommelse, Discord,
valgfrit AI-trin og dashboard.
