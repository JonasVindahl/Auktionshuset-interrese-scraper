# Ændringslog

Formatet følger [Keep a Changelog](https://keepachangelog.com/da/1.1.0/), og
versionerne er [semantiske](https://semver.org/lang/da/).

## [Unreleased]

### Tilføjet

- `LICENSE`: MIT.

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
