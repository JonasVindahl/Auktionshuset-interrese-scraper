# Agent-noter: Auktionshuset Hunter

Dansk projekt. Skriv kode, kommentarer, commit-beskeder og docs på dansk.

## Kommandoer

```bash
.venv/bin/python -m pytest                        # hele suiten
.venv/bin/python -m pytest tests/test_corpus.py   # kun facitlisten
.venv/bin/python -m auction_hunter scan           # se fund, gem/send intet
.venv/bin/python -m auction_hunter check          # konfig + Discord-test
.venv/bin/python -m auction_hunter dump-config    # fuld effektiv konfig
.venv/bin/python -m auction_hunter web            # dashboard på :8080
.venv/bin/python -m pytest tests/test_web_*.py    # kun dashboardet
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

**Webben må kun skrive tre steder.** Dashboardet skriver til `feedback`, til
`config/interests.yml` og til sin egen samtalefil `data/conversations.db`
(`web/chatstore.py`). Alt andet ejer agenten. Samtalerne ligger i en separat
fil, netop fordi webben ikke må kunne røre agentens hukommelse. Sider læser
gennem `queries.ro_conn`, som åbner agentens database i read-only-tilstand, så
en fejl i en rute ikke kan ødelægge hukommelsen.

**Sprogmodellen i `web/chat.py` skriver aldrig SQL.** Den leverer et
struktureret filter med samme felter som søgeformularen, og Python bygger
forespørgslen. Uden den grænse ville en prompt-injektion i en lot-titel kunne
nå databasen. `SearchQuery.normalized()` retter ugyldige værdier til deres
standard i stedet for at fejle.

**`interests.yml` redigeres linje for linje.** `web/yamledit.py` rører kun de
linjer der skal ændres. Kommentarerne bærer beslutningerne, og en
PyYAML-round-trip ville smide dem væk — det var derfor `tools/tune.py` blev
fjernet. Testene i `tests/test_web_yamledit.py` vogter at en tilføjelse
efterfulgt af en fjernelse giver en byte-identisk fil.

**`distinct_forms` dedupliker varianter.** `strømforsyning` og
`stroemforsyning` er samme ord; uden dedup omgik de to-træfs-reglen.

**AI-trinnet er fail-open.** `classifier.py` må kun kunne undertrykke fund når
modellen svarer gyldigt. Netværksfejl, timeout, ulæseligt svar og manglende
nøgle skal alle føre til at fundet sendes videre. Et teknisk problem må ikke
koste et rigtigt fund — det er den ene fejl der er dyrere end støj.

**Kun nye fund sendes til AI.** `run_once` kalder klassificeringen på de matches
der ikke er notificeret før, og højst `max_per_run` pr. kørsel. Fund ud over
loftet udskydes til næste kørsel; de sendes aldrig ufiltreret.

## Web-UI

**Ingen emoji i brugerrettede flader.** Ikoner er inline SVG fra
`templates/_icons.html`. Kategoriernes `emoji`-felt i `interests.yml` læses
ikke længere af skabelonerne, men feltet er urørt.

**Alt visuelt står i `DESIGN.md` og som tokens i `static/app.css`.** Læs
DESIGN.md før du rører farver, typografi, spacing eller bevægelse; den forklarer
hvorfor tallene er som de er, og hvilke regler der ikke må brydes.

Kort: UI-skriften er **Onest** (variabel, selvhostet) og display-skriften
**Bricolage Grotesque**, som kun bruges over 24 px. Plus Jakarta Sans blev målt
og forkastet, fordi dens mellemrum er 0,17 em og fik "Slutter om 1 dag 8 t" til
at læse som "Slutterom 1 dag 8 t". Der er fire tekstniveauer, og `--text-4` må
**kun** bruges til ikoner og streger; al tekst skal være mindst `--text-3`
(4,6:1). Den lyse primærknap bruger `--accent` med `--on-accent`, fordi
brand-amberen med hvid tekst kun giver 2,3:1. Kategorifarverne kommer fra
Okabe-Ito og har en prik-variant til grafik og en tekstvariant til etiketten.

**Bevægelse er en del af systemet.** Varigheder og kurver er tokens
(`--dur-*`, `--ease-*`). Der animeres kun `transform` og `opacity`, aldrig
`transition: all` eller layout-egenskaber. Kort får en forskudt indgang via
`--i`, som sættes i skabelonen og begrænses til 9.

**Fire ting i webfladen har egne kontrakter.** `data-view` på `<html>` skifter
mellem `katalog` og `kompakt` og huskes i localStorage under `visning`.
Tastaturgenvejene (j/k/Enter/x/w/b/c/?/Esc) ligger i `app.js`, markerer med
`.is-selected` og hjælpearket er `#key-help`. Hvert kort kan have
`row.series`, som er lot'ets prisforløb fra `price_history` og tegnes som en
inline SVG-kurve. `/drift` samler kørsler, størrelser og tilstand; den viser
kun om en hemmelighed er sat, aldrig værdien.

**Lot-siderne er et ekstra kald og er slået fra.** details.py henter lot-siden
for hvert fund og leder efter danske signaler om stand. Det er slået fra i
interests.yml, fordi det lægger et kald oven i dem en kørsel allerede laver.
Kolonnerne details, details_flags og details_at kom med en migration, så webben
skal læse dem gennem has_column — containeren kan starte før agenten har
migreret, og en manglende kolonne må ikke give en 500.

**Kortets struktur.** `.card` er billede plus indhold, og `.card-row` holder
fakta og handlinger på samme linje, så højden styres af billedet og der ikke
opstår et tomt bånd i bunden.

**Kontrakten mellem skabelon, CSS og `app.js` er id'er og data-attributter:**
`#cards-container`, `#flat-list`, `#no-results`, `#visible-count`,
`#filter-toggle`, `#filter-panel`, `#ending-chip`, `#price-min`,
`#price-max`, `#reset-btn`, `#active-filters` samt `data-cat`, `data-ts`,
`data-price`, `data-endsin`, `data-lot`, `data-feedback`, `data-action`,
`data-lot-id`, `data-cat-key`, `data-period`, `data-sort`,
`data-cat-block`, `data-autosubmit` og `data-search`. Ingen test dækker
`app.js`, så et omdøbning ser grøn ud i CI og er død i browseren.

**Sortering kloner, flytter ikke.** Ved anden sortering end 'nyeste' klones de
synlige kort ind i `#flat-list`; originalerne bliver stående i deres
datogrupper. Flytter man dem i stedet, forsvinder de fra grupperne når man
skifter tilbage til 'nyeste'.

**Progressiv afsløring er standarden.** Filtre, kategori-blokke, niveauer og
tilføj-formularer ligger bag `<details>` og virker uden JavaScript. Alle
arkivfiltre er stadig server-side via GET-formularen.

**Alt der kan tage tid skal vise at det arbejder.** Mest tydeligt i assistenten,
hvor hele turen kører modelkald server-side før redirect: imens vises et skelet
i tråden (`#chat-pending`) og en tynd streg i toppen (`#busy-bar`). Mønsteret er
generelt: sæt `data-busy` på en formular eller et link, så tager
`initBusyStates` i `app.js` sig af resten. Vi sætter ikke `disabled` på
knapperne, for en knap med `name`/`value` bliver så ikke sendt med;
`pointer-events` i CSS holder dobbeltklik ude i stedet.

**Ingen inline JavaScript i skabelonerne.** CSP'en tillader kun script fra
`/static`. Bekræftelser ligger på `data-confirm` og håndteres af en delegeret
`submit`-lytter, og billedfejl fanges af en global `error`-lytter i
capture-fasen. En inline `onsubmit` med et nøgleord indsat ville desuden kunne
bryde ud af JavaScript-strengen, fordi browseren HTML-dekoder attributten før
JS-parsing.

**Adgangskoden fejler lukket.** Kan `WEB_PASSWORD` ikke læses, rejser
`auth.configured_password` en `AuthConfigError` som giver 503. Forskellen
mellem "ingen adgangskode er sat" og "adgangskoden kunne ikke læses" er hele
pointen; uden den står dashboardet åbent på en tastefejl i en filsti.

**Søgeparametre clampes i `SearchQuery.normalized`.** En formular kan sende
hvad som helst, og et prisloft over 2^63 får sqlite3 til at kaste mens et
`days_back` på 740000 løber tør for datoer. Det skal give en tom søgning, ikke
en 500.

**Dansk tid og danske månedsnavne.** `formatting.LOCAL_TZ` er
Europe/Copenhagen, fordi containeren kører UTC, og månedsnavnene kommer fra en
fast liste fordi `strftime("%B")` følger systemets locale.

**Kategori-etiketter er fælles for siderne.** `app.category_labels()` giver
`key -> label` fra `interests.yml`, og `app.filter_chips()` bygger de
fjernbare filtre på arkivsiden. Begge fejler blødt, hvis konfigurationen ikke
kan læses.

## Testfilosofi

`config/match_expectations.jsonl` er projektets vigtigste artefakt.
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
  "ingen fund i dag" i det uendelige. Oprydning må aldrig røre `notifications`;
  den tabel alene er dedup'ens grundlag (`already_notified` slår kun op der).
  `lots` kan i princippet ryddes for afsluttede lots uden at gensende noget,
  men den er arkivets indhold, og arkivet er hele pointen med søgefanen.
- **`price_history.observed_at` har mikrosekunder.** Primærnøglen er
  `(lot_id, observed_at)`, så sekund-præcision tabte prisændringer der faldt i
  samme sekund.
- **Containeren kører som UID/GID 10001**, men `./data` oprettes af værten.
  Rettigheden skal sættes manuelt (`chown -R 10001:10001 data reports`), ellers
  fejler SQLite med "unable to open database file" ved første kørsel.
- **`sqlite3.Row` har ingen `.get()`.** Den understøtter `row["kolonne"]` og
  `row.keys()`, men ikke dict-metoder. `row.get(...)` kaster `AttributeError`
  og nåede produktion én gang, fordi `web.py` ikke havde tests. Brug
  `row["x"] if "x" in row.keys() else fallback`.
- **`ends_at` kan ikke sammenlignes i SQL.** Feltet gemmes som ISO med offset
  (`2026-09-16T14:30:00+02:00`), mens `datetime('now')` giver
  `2026-09-16 12:05:24`. `T` sorterer efter mellemrum, så en
  strengsammenligning melder at alt ligger i fremtiden. Filtrér i Python med
  `_parse_dt`, ikke i en `WHERE`-klausul.
- **En ny kolonne kræver en migration.** `CREATE TABLE IF NOT EXISTS` rører
  ikke en eksisterende tabel. Nye kolonner tilføjes i `MIGRATIONS` i
  `storage.py` og køres ved hver opstart af `Store`.
- **Billeder er lazy-loadede.** `img.src` er ofte en base64-pladsholder, og den
  rigtige adresse står i `data-src` eller `srcset`. `Scraper._image_url`
  håndterer begge og gør relative adresser absolutte. Er billedfeltet tomt i én
  kørsel, beholder `record_lot` det gamle i stedet for at overskrive med tomt.
- **Databasen vokser langsomt nok til at den ikke skal ryddes.** 836 bytes pr.
  lot i alt (623 i `lots`, 281 i `lots_fts`, resten i historik). Ved ~200 nye
  lots om dagen er det ~58 MB efter et år og under 300 MB efter fem. Billeder
  gemmes ikke — kun deres adresse, 120 bytes. Byg ikke oprydning af `lots`
  uden at måle først.
- **De 48 timer på /expired er et visningsvindue, ikke en sletning.** Intet
  fjernes efter 48 timer; fanen viser bare det vindue hvor det giver mening at
  markere «budt/købt».
- **Billedadressen dør når lot'et lukker.** Derfor henter `images.py`
  miniaturen mens lot'et er aktivt, og kun for lots i `notifications` eller
  `review_queue` — ikke for de ~2.200 der scrapes. Filnavnet er et hash af
  `lot_id`, som kommer fra et HTML-attribut og aldrig må bruges som filnavn
  direkte. `looks_complete` tjekker at filen har sin kendte afslutning, fordi
  en afbrudt overførsel stadig har de rigtige magiske bytes i starten.
- **Søgeindekset har to danske foldninger.** `normalize` giver `hoejttaler`,
  `normalize_loose` giver `hojttaler`. Begge indekseres i `lots_fts.normalized`,
  fordi folk skriver begge dele. Brug aldrig `normalize_loose` til
  nøgleordsmatchning — den er for upræcis til at afgøre om et lot er
  interessant.
- **Klonede kort i frontenden må ikke tælles med.** `app.js` kloner synlige
  kort ind i `#flat-list` ved anden sortering end 'nyeste'. Alle opslag skal
  scopes til `#cards-container`, ellers vokser listen for hvert klik.
- **Web-afhængighederne er valgfri.** Agenten skal kunne importeres uden
  FastAPI; derfor importerer `web/__init__.py` dovent, og `cli.cmd_web` fanger
  `ImportError` med en brugbar besked.
- Scraperen henter **kun titler** fra listen. Beskrivelsen kan hentes fra
  lot-siden, men det er slået fra som standard og er et ekstra kald pr. lot.
- Katalogget må ikke hentes oftere end hvert 15. minut. Bemærk at ét
  *interval* ikke er ét *kald*: en kørsel henter auktionslistens sider (én gang
  pr. landdel, fordi regionen ikke står på auktionskortet) plus mindst ét
  katalogkald pr. auktion. Antallet står på `/drift` pr. kørsel og i
  `runs.requests`, så det kan efterprøves i stedet for at blive anslået
  (`MIN_SCRAPE_INTERVAL_SECONDS`). Sænk den ikke, og undgå healthchecks der
  rammer netværket.
- **Auktionsinfo hentes gratis fra kataloget.** Adresse, levering, eftersyn og
  udlevering står i `dropdown-body`-panelet på katalogsiden, som allerede
  hentes for at få lot'ene. `_parse_auction_info` læser det; landet, typen,
  adressen og `shipping` gemmes pr. lot ligesom `auction_title`.
- **Hjemlandsdele og levering.** `region_ids` styrer hvad der hentes,
  `local_region_ids` hvad man selv kan køre til. `match_lot` dropper et lot
  uden for hjemlandsdelene medmindre `shipping` er sand. Ukendt region ('' eller
  et ukendt navn) slippes igennem, så et ændret HTML-udtræk ikke koster fund, og
  uden `local_region_ids` er alle valgte regioner lokale og reglen inaktiv.
- `Dockerfile` kopierer kun `src/` og `config/` — `tests/` og `tools/` er ikke
  i imaget.

## Konventioner

- Frossen dataclass til data (`Lot`, `Auction`, `Category`).
- Type hints overalt; `from __future__ import annotations`.
- Kode på engelsk, brugerrettet tekst og docs på dansk.
- Kommentarer forklarer *hvorfor*, ikke hvad koden gør.
- **Tomme formularfelter er ikke ugyldige tal.** En HTML-formular sender hvert
  felt med, også de tomme. Et `int | None`-parameter i FastAPI afviser `""` med
  422, så en helt almindelig søgning uden prisfilter fejlede. Brug `OptionalInt`
  fra `web/app.py` til alle heltalsfelter der kommer fra en formular, og bemærk
  at `Query(...)` som *default* overskriver Annotated-metadataen — `Query` skal
  ind i `Annotated[...]` når typen har en `BeforeValidator`.
- **Test formularer som browseren sender dem.** Testene ramte ikke fejlen
  ovenfor, fordi de sendte enkeltparametre med rigtige værdier. En formular
  sender *alle* felter, inklusive de tomme. `BROWSER_FORM` i
  `tests/test_web_app.py` er den form der skal testes mod.
