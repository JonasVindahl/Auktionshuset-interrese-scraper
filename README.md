# Auktionshuset Hunter

Overvåger aktive auktioner på [auktionshuset.dk](https://auktionshuset.dk) for
Sjælland, matcher dem mod en interesseprofil og sender fund til Discord.
Agenten husker hvilke lots den har set, så du kun får besked om noget nyt
eller prisændringer — ikke de samme varer hver 15. minut.

## Hvad den gør

Hvert 15. minut (auktionshusets vilkår tillader ikke hurtigere):

1. Henter lot-listen for Sjælland, filtreret til aktive auktioner
2. Oversætter titler til normaliseret tekst — `Højttaler` og `hoejttaler` er
   samme ord
3. Matcher mod interesseprofilen i `config/interests.yml`
4. Fratrækker udelukkelser, så høretelefoner og tastaturer ikke støjer
5. Slår op i SQLite-hukommelsen og beholder kun nye lots og prisændringer
6. Sender fund til Discord som embeds, højst 10 pr. besked

## Kom i gang

### Lokalt

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # udfyld DISCORD_WEBHOOK_URL
.venv/bin/python -m auction_hunter check        # viser konfiguration, tester Discord
.venv/bin/python -m auction_hunter scan         # vis fund uden at gemme eller sende
.venv/bin/python -m auction_hunter once         # én rigtig kørsel
.venv/bin/python -m auction_hunter run          # kør i loop
```

`scan` er den sikre start: den hverken gemmer i databasen eller sender til
Discord, så du kan se hvad profilen fanger, før du lader den løbe.

### Docker

```bash
cp .env.example .env        # udfyld DISCORD_WEBHOOK_URL
docker compose up -d --build
docker compose logs -f
```

Containeren kører som ikke-root med read-only filsystem og uden åbne porte —
den henter kun data ud. Hukommelsen og profilen ligger i `./data` og
`./config` på værten, så de overlever genopbygning.

## Konfiguration

Al adfærd styres fra `config/interests.yml`, som læses på ny ved **hver**
kørsel. Ændringer slår igennem uden genstart.

### Nøgleord i fire niveauer

Opdelingen afspejler hvor meget et ord kan bære af betydning alene:

| Niveau | Eksempel | Regel |
|---|---|---|
| `strong` | `højttaler`, `espressomaskine`, `proxmark` | Matcher alene — det er en konkret vare |
| `weak` | `server`, `stereo`, `rack` | Kræver mindst to **forskellige** træffere |
| `brands` | `sennheiser`, `synology`, `seiko` | Kræver en produkttype ved siden af |
| `exact` | `mission`, `rel`, `quad` | Matcher kun som selvstændigt ord |

`brands` er det vigtigste niveau. Et mærkenavn alene siger nemlig intet:
`Div. batterier SENNHEISER` og `Flightcase SENNHEISER` er ikke HiFi, og
`Div. Computer reserverdele ... SYNOLOGY` er ikke IT-udstyr. De kræver derfor
et ord som `forstærker` eller `nas` ved siden af.

`exact` findes fordi nogle mærker også er almindelige orddele. `mission` er et
højttalermærke, men `transmission` er et andet ord.

### Udelukkelser

`exclude` vinder over alt andet. Brugeren er eksplicit ligeglad med
høretelefoner, tastaturer og mus, og tilbehør som flightcases, batterier og
rackkufferter støjer mere end de gavner, fordi de optræder i bunker.

Bemærk at udelukkelsen af `høretelefoner` ikke rammer en
*høretelefonforstærker* — det er en anden vare, og den er interessant.

### Budget

Prisloftet er blødt: fund over grænsen rapporteres stadig, men markeres
`[OVER]`, indtil de passerer `max_price * soft_over_budget_factor`.

### Regioner

Region-id'er står i `config/interests.yml`. Sjælland er `lyr0boj4d8`. Skift
`source.region_ids` for at dække flere landsdele.

## Hemmeligheder

Discord-webhooken læses af `src/auction_hunter/secrets.py` på tre måder. Den
første der er sat vinder:

| Metode | Miljøvariabel |
|---|---|
| Direkte | `DISCORD_WEBHOOK_URL` |
| Fra fil (Docker secrets) | `DISCORD_WEBHOOK_URL_FILE=/run/secrets/discord_webhook` |
| Indirekte | `DISCORD_WEBHOOK_URL_FROM_ENV=MIN_VARIABEL` |

Webhooken logges aldrig i klartekst. `.env` er i `.gitignore`.

## Test

```bash
.venv/bin/python -m pytest
```

To slags tests:

- **Enhedstests** i `tests/test_textmatch.py` og `tests/test_matcher.py` bruger
  syntetiske titler og tester reglerne isoleret.
- **Facitlisten** i `tests/corpus/match_expectations.jsonl` indeholder rigtige
  lot-titler med et menneskeligt svar: `yes`, `no` eller `maybe`.

Facitlisten er den vigtigste artefakt. Auktionerne skifter indhold hele tiden,
men filen gør det ikke — den er den versionerede beslutning om hvad der er
interessant. Derfor kan `interests.yml` tunes uden at jagte et tilfældigt
øjebliksbillede: ændringer valideres mod et fast facit. `yes` og `no` er hårde
krav, mens `maybe` er grænsetilfælde hvor begge svar er forsvarlige.

Tilføj nye sager til facitlisten når du ser en fejl i praksis:

```json
{"title": "Rigtig titel fra auktionen", "expect": "no", "why": "Hvorfor"}
```

### Måling mod et rigtigt snapshot

`tools/evaluate.py` kører matcheren mod et gemt snapshot og viser hvilke
nøgleord der udløser flest fund — nyttigt til at se om et ord er for bredt:

```bash
.venv/bin/python -m auction_hunter export --out tests/fixtures/lots_sample.json
.venv/bin/python tools/evaluate.py
```

Snapshottet er bevidst ikke i git, da det er et øjebliksbillede. Testene kører
uden det.

## Arkitektur

| Fil | Ansvar |
|---|---|
| `scraper.py` | Henter og parser lot-lister |
| `textmatch.py` | Normalisering, accent-folding, sammensatte ord |
| `config.py` | Læser og validerer `interests.yml` |
| `matcher.py` | Kobler lots til kategorier |
| `storage.py` | SQLite-hukommelse: sete lots, priser, notifikationer |
| `notifier.py` | Bygger og sender Discord-embeds |
| `runner.py` | Kører loopet og binder delene sammen |
| `secrets.py` | Læser hemmeligheder fra miljø, fil eller indirekte |
| `fees.py` | Beregner bud og samlet pris inkl. gebyr |
| `cli.py` | Kommandolinjen |

Hukommelsen ligger i SQLite (`data/auction_hunter.db`) og gør tre ting: den
forhindrer gentagne notifikationer, den fanger prisændringer, og den giver
`stats` og `export` noget at rapportere om.

## Licens

Ingen — privat projekt.

