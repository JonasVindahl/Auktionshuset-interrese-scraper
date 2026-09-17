# Deployment

Hvad der skal være på plads for at køre Auktionshuset Hunter i drift, og hvad
der stadig mangler. Skrevet til det faktiske setup: Docker Compose på en vært,
dashboardet bag en reverse proxy (Pangolin/Traefik).

## 1. Hurtig start

    cp .env.example .env          # udfyld det der er markeret nedenfor
    mkdir -p data config reports
    chown -R 10001:10001 data reports   # containeren kører som UID 10001
    docker compose up -d --build

To tjenester startes: `hunter` (agenten i loop) og `web` (dashboardet på 8080).
De deler `./data` og `./config`.

## 2. Påkrævet konfiguration

| Variabel | Hvorfor | Hvis den mangler |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Fund sendes hertil | Agenten finder fund, men siger intet |
| `WEB_PASSWORD` | Dashboardet kan ændre profilen | Siden står åbent for alle der kan nå den |
| `WEB_SECRET_KEY` | Signerer login-cookien | Tilfældig nøgle ved opstart, så du logges ud ved hver genstart |
| `REGION_IDS` | Hvilke landsdele der følges | Falder tilbage til `config/interests.yml` |

Webhook og nøgler kan i stedet læses fra en fil (`*_FILE`) eller fra en anden
miljøvariabel (`*_FROM_ENV`), så de kan ligge i en Docker-secret.

## 3. Regioner

`region_ids` i `config/interests.yml` (eller `REGION_IDS` i miljøet) styrer
hvilke landsdele agenten ser på. Auktionshusets fem landsdele ligger i
`regions`-mappingen.

    region_ids: all          # alle landsdele fra mappingen
    region_ids:
      - lyr0boj4d8           # kun Sjælland
      - epV0wO0K7l           # Fyn

Etiketten i beskederne udledes automatisk ("Hele Danmark" for alle) og kan
overstyres med `REGION_LABEL`. Uden det ville varslerne påstå "Sjælland" mens
agenten fulgte hele landet.

## 3b. Profiler

En profil er et navngivet interessesæt: sine egne kategorier, sit eget prisloft,
sine egne udelukkelser og sin egen webhook. Uden `profiles` i `interests.yml` er
der én implicit standardprofil, og alt kører som før.

    profiles:
      hifi:
        label: HiFi og lyd
        categories: [audio_hifi]
        max_price: 2500
        webhook_env: DISCORD_WEBHOOK_HIFI
      vaerktoj:
        label: Værktøj og maker
        categories: [maker_electronics, it_tech]
        max_price: 800
        exclude: [bil, trailer]

- `categories: null` (eller udeladt) betyder alle kategorier; `[]` betyder ingen.
- Globale `exclude`-ord gælder altid; profilens ord lægges oveni.
- En profil kan slukkes med `enabled: false` eller `PROFILE_<NØGLE>_ENABLED=0`.
  Brug ascii-nøgler, da nøglen også bruges i miljøvariabelnavnet.
- Dedup er pr. (lot, kategori, profil), så det samme lot kan give én besked pr.
  profil, men aldrig to til den samme.
- `webhook_env` navngiver en miljøvariabel med en webhook. Er den sat, sendes
  profilens fund der; ellers bruges `DISCORD_WEBHOOK_URL`.
- Ukendte kategorinavne i en profil stopper opstarten med en fejl i stedet for
  at matche stille på ingenting.

## 4. Reverse proxy

Dashboardet bør ikke eksponeres direkte. Bind det til lokalhost og lad
Pangolin/Traefik tage trafikken:

    ports:
      - "127.0.0.1:8080:8080"

Appen læser nu proxy-headere, så den ser `https` og den rigtige klient-IP når
den står bag Pangolin/Traefik. Sæt `FORWARDED_ALLOW_IPS` til proxyens adresse,
ellers ignoreres headerne.

Tre variabler styrer resten:

    WEB_BASE_URL=https://hunter.jonasvindahl.dk   # => login-cookien kraever HTTPS
    ALLOWED_HOSTS=hunter.jonasvindahl.dk          # afvis andre Host-headere
    FORWARDED_ALLOW_IPS=172.16.0.1                # trafik fra proxyen

Kører du uden TLS på LAN, så lad `WEB_BASE_URL` og `WEB_COOKIE_SECURE` være
usat, lad `ALLOWED_HOSTS` være tom, og bind porten til `127.0.0.1` i stedet.

## 5. Backup og gendannelse

Det der skal reddes: `data/auction_hunter.db`, `data/conversations.db` og
`data/images/`. `config/interests.yml` ligger i git.

SQLite kører med WAL, så en rå filkopi kan være ufuldstændig. Tag backup med
SQLite selv, eller stop `web` og `hunter` først:

    docker compose stop web hunter
    sqlite3 data/auction_hunter.db ".backup 'backup/auction_hunter.db'"
    sqlite3 data/auction_hunter.db "PRAGMA integrity_check;"
    docker compose start

Backup-stien i homelabben er Proxmox Backup Server og TrueNAS NFS.

## 6. Opgradering og rollback

    git pull
    docker compose build
    docker compose up -d

Migrationer kører ved opstart af `Store`, så en ny kolonne tilføjes automatisk.
`web` læser gennem `has_column`, så den kan starte før agenten har migreret.

Rollback: byg det forrige commit, og gendan databasen fra backup hvis skemaet
blev ændret.

## 7. Overvågning

- `/healthz` — liveness. Svarer hvis processen og databasen virker. Rammer ikke
  auktionshuset.
- `/readyz` — klarhed. Kræver også at `interests.yml` kan læses, så containeren
  ikke meldes klar med et tomt matchgrundlag.
- `/drift` — kørsler, størrelser, konfiguration, version og hemmelighedernes
  tilstand (kun sat/ikke sat, aldrig værdien).
- Blindheds-advarsel: falder antallet af lots under halvdelen af normalen,
  sender agenten en Discord-besked. Det er den fejl der ellers først opdages
  uger senere.

Sæt Uptime Kuma til at ramme `/readyz` frem for `/`.

## 8. Sikkerhed: status

På plads: ikke-root (UID 10001), read-only rootfs, `cap_drop: ALL`,
`no-new-privileges`, CSP, `X-Frame-Options`, `Referrer-Policy`, `no-store` på
HTML, adgangskode sammenlignet i konstant tid, fail-closed hvis adgangskoden
ikke kan læses, hemmeligheder via fil/miljø, og login-throttling (5 forsøg pr.
15 minutter pr. klient-IP).

Bemærk at throttlingen tæller på `request.client.host`. Bag en reverse proxy
er det proxyens adresse, så grænsen bliver global i stedet for per klient. Det
er acceptabelt for et enkeltbruger-dashboard, men forsvinder først når
proxy-headers er på plads.

CSRF er dækket i to lag uden tokens: sessionen er `SameSite=Lax`, CSP'en har
`form-action 'self'`, og usikre metoder afvises hvis `Origin`/`Referer` peger på
en anden vaert. `SameSite=Lax` alene blokerer cross-site POST, `form-action`
dækker formularer, og oprindelsestjekket dækker også `fetch()`-kaldet til
`/feedback`, som `form-action` ikke omfatter.

Mangler: begrænsning af hvilken adresse porten binder til i compose, og
OCI-labels og digest-pinning af base-imaget.

## 9. Test, lint og release

    python -m pytest        # hele suiten
    ruff check src tests

CI kører tests på Python 3.11 og 3.13, ruff og et Docker-build ved hvert push.
Versionen ligger ét sted (`auction_hunter.__version__`) og vises i `/healthz`,
`/readyz`, sidefoden og på `/drift`.

## 10. Kendte mangler

- Ingen `LICENSE` i repoet.
- `DESIGN.md` og `README.md` beskriver et ældre udtryk end koden.
- Intet værktøj til backup/restore (kun kommandoerne ovenfor).
- Ingen metrics-endpoint; `/drift` er manuel.
- Ens bruger og én adgangskode. Der er ingen profiler pr. bruger endnu.
