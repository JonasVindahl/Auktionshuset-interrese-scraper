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
6. Lader en sprogmodel vurdere de nye fund (valgfrit, se nedenfor) — støj
   afvises, og grænsetilfælde lægges i et samlet digest
7. Sender fund til Discord som embeds, højst 10 pr. besked

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

Containeren kører som UID/GID 10001, mens `./data` oprettes af dig. Skriv
derfor til mappen, ellers fejler SQLite på "unable to open database file":

```bash
mkdir -p data reports
sudo chown -R 10001:10001 data reports
```

Er UID/GID optaget på værten, kan de sættes ved build:
`docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)`.

## Konfiguration

Al adfærd styres fra `config/interests.yml`, som læses på ny ved **hver**
kørsel. Ændringer slår igennem uden genstart.

### Nøgleord i fire niveauer

Opdelingen afspejler hvor meget et ord kan bære af betydning alene:

| Niveau | Eksempel | Regel |
|---|---|---|
| `strong` | `højttaler`, `espressomaskine`, `proxmark`, `server` | Matcher alene — det er en konkret vare |
| `weak` | `router`, `stereo`, `rack` | Kræver mindst to **forskellige** træffere |
| `brands` | `sennheiser`, `synology`, `seiko` | Kræver en produkttype ved siden af |
| `exact` | `mission`, `rel`, `quad` | Matcher kun som selvstændigt ord |

`brands` er det vigtigste niveau. Et mærkenavn alene siger nemlig intet:
`Div. batterier SENNHEISER` og `Flightcase SENNHEISER` er ikke HiFi, og
`Div. Computer reserverdele ... SYNOLOGY` er ikke IT-udstyr. De kræver derfor
et ord som `forstærker` eller `nas` ved siden af.

`exact` findes fordi nogle mærker også er almindelige orddele. `mission` er et
højttalermærke, men `transmission` er et andet ord.

Et ord som `server` er flyttet til `strong`, fordi det i en auktionssammenhæng
næsten altid er en maskine. Prisen er at en bøjning som `kaffeservering` ikke
fanges — den afvejning er bevidst.

### Server og storage

Servere, NAS og drev er en selvstændig interesse, så `it_tech` har et højere
prisloft end de øvrige kategorier (7.000 kr mod 1.000 kr). En stak på 12-30
diske ligger typisk på 4.000-11.000 kr i hammerpris, altså op til omkring
16.500 kr færdigpris inkl. moms og salær.

Ordet `disk` er **bevidst ikke** et nøgleord. I de rigtige data rammer det
`ekspeditionsdisk`, `købmandsdisk` og `Industriopvaskemaskine WEXIÖDISK` — altså
butiksinventar og hårde hvidevarer. I stedet bruges `harddisk` og de øvrige
sammensætninger, inklusive auktionshusets egen stavefejl `hardisk`.

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

## AI-trin (valgfrit)

Nøgleord er gode til at finde kandidater billigt, men de kan ikke læse
kontekst. De fanger derfor titler som
`Div. lydudstyr: DVI-forlængere, HDD-adaptere mv.`, fordi ordet `hdd` står der,
selvom varen er et rodlot med adaptere.

AI-trinnet lægger en sprogmodel ovenpå: **kun lots der allerede har matchet på
nøgleord** vurderes, så omkostningen følger antallet af fund og ikke de ~2.200
lots. Modellen svarer i tre kategorier:

| Svar | Effekt |
|---|---|
| `ja` | Sendes med det samme, som før |
| `nej` | Undertrykkes |
| `måske` | Lægges i kø og sendes samlet i ét digest — ikke i nuet |

Profilen modellen vurderer ud fra står i `config/interests.yml` under
`classifier.profile`. Det er den egentlige "smag", mens nøglelisterne kun er
grovsortering.

Slå til med:

```bash
# config/interests.yml:  classifier.enabled: true
export CLASSIFIER_API_KEY=sk-...
```

Trinnet er **fail-open**: kan modellen ikke nås, eller svarer den ulæseligt,
beholdes fundet og sendes videre. Et teknisk problem må ikke koste et rigtigt
fund. Mangler nøglen helt, kører agenten videre uden AI.

Enhver OpenAI-kompatibel endpoint virker (`CLASSIFIER_BASE_URL`), fx OpenAI,
Azure, OpenRouter, Ollama eller vLLM. Der er ingen ny afhængighed — kaldet går
gennem `requests`.

Svarene caches i SQLite på et hash af titel, kategori, model og
`CLASSIFIER_VERSION`. Et ændret lot eller en ændret prompt giver derfor nye
opslag, mens alt andet genbruges. Køen af `måske`-fund kan ses med:

```bash
python -m auction_hunter review
python -m auction_hunter stats      # viser AI-fordelingen
```

## Hemmeligheder

Discord-webhooken læses af `src/auction_hunter/secrets.py` på tre måder. Den
første der er sat vinder:

| Metode | Miljøvariabel |
|---|---|
| Direkte | `DISCORD_WEBHOOK_URL` |
| Fra fil (Docker secrets) | `DISCORD_WEBHOOK_URL_FILE=/run/secrets/discord_webhook` |
| Indirekte | `DISCORD_WEBHOOK_URL_FROM_ENV=MIN_VARIABEL` |

Webhooken logges aldrig i klartekst. `.env` er i `.gitignore`.

AI-nøglen følger samme mønster: `CLASSIFIER_API_KEY`,
`CLASSIFIER_API_KEY_FILE` eller `CLASSIFIER_API_KEY_FROM_ENV`. Er
`CLASSIFIER_API_KEY` ikke sat, bruges `OPENAI_API_KEY` i stedet.

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

### Drift over lang tid

Auktionerne skifter indhold hele tiden, så agenten er bygget til at køre i
månedsvis. Tre ting kunne ellers gå galt:

**Databasen voksede i det uendelige.** Pris-historikken blev skrevet hver gang,
også når prisen var den samme, altså en ny række pr. lot hvert 15. minut. Det
gav omkring 3-8 GB om året. Nu skrives der kun ved en faktisk ændring, og
historik ældre end 180 dage ryddes dagligt. Det giver omkring 450 MB om året —
og langt mindre i praksis, da tallet forudsætter at alle 2.208 lots bliver
liggende i et helt år.

Oprydningen rører bevidst ikke `lots` og `notifications`. Det er selve
hukommelsen: sletter man den, gensender agenten gamle fund, eller mister evnen
til at genkende dem som sete før. Kun historik og afsluttede kørsler ryger.

**Et lot kunne miste en prisændring.** Primærnøglen i `price_history` er
`(lot_id, observed_at)`. Med sekund-præcision overskrev to ændringer i samme
sekund hinanden. Tidsstemplet har derfor mikrosekunder.

**Agenten kunne blive blind uden at sige det.** Den farligste fejl: ændrer
auktionshuset deres HTML, returnerer `fetch_lots` tomt, og kørslen melder
succes med 0 fund. Man opdager det først, når man undrer sig over at der ikke
har været noget i ugevis. Agenten sammenligner derfor antallet af lots med sin
seneste normale kørsel og sender en Discord-besked, hvis det styrter sammen —
eller hvis der slet ikke findes aktive auktioner længere. Advarslen sendes
højst én gang i døgnet, så den ikke selv bliver spam.

## Licens

Ingen — privat projekt.

