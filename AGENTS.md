# Agent-noter: Auktionshuset Hunter

Dansk projekt. Skriv kode, kommentarer, commit-beskeder og docs på dansk.

## Kommandoer

```bash
.venv/bin/python -m pytest                        # hele suiten
.venv/bin/python -m pytest tests/test_corpus.py   # kun facitlisten
.venv/bin/python -m auction_hunter scan           # se fund, gem/send intet
.venv/bin/python -m auction_hunter check          # konfig + Discord-test
.venv/bin/python -m auction_hunter dump-config    # fuld effektiv konfig
PYTHONPATH=src .venv/bin/python tools/evaluate.py # mål mod snapshot
```

Virtuelt miljø er `.venv`. `pytest.ini` sætter `pythonpath = src`.

## Arkitekturregler

**Nøgleord har fire niveauer, og niveauet er en semantisk beslutning, ikke en
smagssag.** Spørg: *kan ordet stå alene?*

- `strong` — konkrete produkttyper. `højttaler`, `espressomaskine`, `proxmark`,
  `server`.
- `weak` — brede ord. `router`, `stereo`, `rack`. Kræver to **forskellige**
  træffere; varianter som `højtaler`/`højtalere` tælles som ét.
- `brands` — mærkenavne. **Et mærke må aldrig matche alene.** `Div. batterier
  SENNHEISER` og `Flightcase SENNHEISER` er ikke HiFi. Kræver et weak-ord ved
  siden af.
- `exact` — mærker der også er orddele. `mission` vs `transmission`, `rel` vs
  `relevans`, `quad` vs `quadcopter`.

Den historiske fejl i projektet var at lægge mærkenavne i `strong`. Det gav
støj i alle kategorier og blev fikset ved at indføre `brands`. Læg aldrig et
mærke i `strong`.

**`exclude` vinder over alt.** Læg støjende ord der, ikke i weak.

**`distinct_forms` dedupliker varianter.** `strømforsyning` og
`stroemforsyning` er samme ord; uden dedup omgik de to-træfs-reglen.

**AI-trinnet er fail-open.** `classifier.py` må kun kunne undertrykke fund når
modellen svarer gyldigt. Netværksfejl, timeout, ulæseligt svar og manglende
nøgle skal alle føre til at fundet sendes videre. Et teknisk problem må ikke
koste et rigtigt fund — det er den ene fejl der er dyrere end støj.

**Kun nye fund sendes til AI.** `run_once` kalder klassificeringen på de matches
der ikke er notificeret før, og højst `max_per_run` pr. kørsel. Fund ud over
loftet udskydes til næste kørsel; de sendes aldrig ufiltreret.

## Testfilosofi

`tests/corpus/match_expectations.jsonl` er projektets vigtigste artefakt.
Auktionsdata skifter konstant, men facitlisten gør ikke — den er den
versionerede beslutning om hvad der er interessant. Tune altid `interests.yml`
mod den, aldrig mod et snapshot.

- `yes`/`no` er hårde krav. `maybe` er grænsetilfælde og fejler ikke testen.
- Tilføj en sag når du ser en fejl i praksis. `why`-feltet er obligatorisk.
- `tests/fixtures/lots_sample.json` er gitignoreret og et øjebliksbillede.
  Enhedstestene må ikke afhænge af den.

## Fælder

- **Rediger `interests.yml` linje for linje.** En PyYAML round-trip smider alle
  kommentarer væk, og de bærer beslutningerne. Derfor blev `tools/tune.py`
  fjernet.
- **`env_file` i `docker-compose.yml` slår `interests.yml`.** En
  `CLASSIFIER_ENABLED=0` i `.env` overskygger `enabled: true` i YAML-filen. Derfor
  er alle AI-nøgler i `.env.example` kommenteret ud.
- **`disk` er ikke et nøgleord.** I rigtige data rammer det `ekspeditionsdisk`,
  `købmandsdisk` og `Industriopvaskemaskine WEXIÖDISK`. Brug `harddisk`/`hardisk`.
- **Indrykning i `interests.yml` er 2 mellemrum for kategori, 4 for niveau, 6
  for nøgleord.** Et nøgleord på forkert niveau læses som et andet felt.
- **`git status` i dette miljø er upålideligt** og kan melde "clean" efter en
  redigering. Verificér med `grep`/`sed` eller `git diff`, ikke med `git status`.
- **Env-variabler lækker mellem terminal-kommandoer i denne session.** Sætter du
  `CONFIG_PATH` eller `DISCORD_WEBHOOK_URL_FILE` i én kommando, ser testene dem i
  den næste, og `test_secrets.py` fejler. Kør `unset` eller start en frisk shell
  før den endelige testsuite.
- **Docker kan ikke bygges i dette miljø** (ingen mount- eller
  iptables-tilladelser, selv med `--iptables=false --bridge=none`). Verificér i
  stedet Dockerfile-antagelserne ved at kopiere `src/` og `config/` til et tomt
  træ, sætte `PYTHONPATH`/`CONFIG_PATH`/`DB_PATH` som i Dockerfile og køre
  `python -m auction_hunter check|stats` derfra.
- **Agenten kører i månedsvis, så langtidsadfærd er en del af korrektheden.**
  Pris-historik skrives kun ved ændring (ellers ~3-8 GB/år), historik ryddes
  dagligt efter 180 dage, og `alert_if_blind` sender en Discord-advarsel hvis
  antallet af lots styrter sammen — ellers ser et brudt HTML-udtræk ud som
  "ingen fund i dag" i det uendelige. Oprydning må aldrig røre `lots` eller
  `notifications`; det er dedup'ens grundlag.
- **`price_history.observed_at` har mikrosekunder.** Primærnøglen er
  `(lot_id, observed_at)`, så sekund-præcision tabte prisændringer der faldt i
  samme sekund.
- **Containeren kører som UID/GID 10001**, men `./data` oprettes af værten.
  Rettigheden skal sættes manuelt (`chown -R 10001:10001 data reports`), ellers
  fejler SQLite med "unable to open database file" ved første kørsel.
- Scraperen henter **kun titler**, ingen beskrivelser. En LLM kan derfor ikke
  vurdere stand eller om et par er komplet.
- Auktionshusets vilkår tillader kun ét scrape hvert 15. minut
  (`MIN_SCRAPE_INTERVAL_SECONDS`). Sænk den ikke, og undgå healthchecks der
  rammer netværket.
- `Dockerfile` kopierer kun `src/` og `config/` — `tests/` og `tools/` er ikke
  i imaget.

## Konventioner

- Frossen dataclass til data (`Lot`, `Auction`, `Category`).
- Type hints overalt; `from __future__ import annotations`.
- Kode på engelsk, brugerrettet tekst og docs på dansk.
- Kommentarer forklarer *hvorfor*, ikke hvad koden gør.
