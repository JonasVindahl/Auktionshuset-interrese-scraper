# Auktionshuset Hunter

Overvåger aktive auktioner på [auktionshuset.dk](https://auktionshuset.dk) for
de landsdele du vælger, matcher dem mod en eller flere interesseprofiler og
sender fund til Discord.
Agenten husker hvilke lots den har set, så du kun får besked om noget nyt
eller prisændringer — ikke de samme varer hver 15. minut.

## Hvad den gør

Hvert 15. minut (auktionshusets vilkår tillader ikke hurtigere):

1. Henter lot-listen for de valgte landsdele, filtreret til aktive auktioner
2. Oversætter titler til normaliseret tekst — `Højttaler` og `hoejttaler` er
   samme ord
3. Matcher mod interesseprofilen i `config/interests.yml`
4. Fratrækker udelukkelser, så høretelefoner og tastaturer ikke støjer
5. Slår op i SQLite-hukommelsen og beholder kun nye lots. Stiger prisen på et
   lot du følger eller har budt på, giver det sin egen besked — højst hver 12.
   time pr. lot, så en travl auktion ikke fylder kanalen (slås fra med
   `PRICE_ALERTS=0`). Er et fulgt lot tæt på hammerslag, kommer der også én
   besked om det (`LAST_CHANCE_ALERTS=0`)
6. Lader en sprogmodel vurdere de nye fund (valgfrit, se nedenfor) — støj
   afvises, og grænsetilfælde lægges i et samlet digest
7. Sender fund til Discord som embeds, højst 10 pr. besked

## Kom i gang

### Lokalt

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # udfyld DISCORD_WEBHOOK_URL
export PYTHONPATH=src
.venv/bin/python -m auction_hunter check        # viser konfiguration, tester Discord
.venv/bin/python -m auction_hunter scan         # vis fund uden at gemme eller sende
.venv/bin/python -m auction_hunter once         # én rigtig kørsel
.venv/bin/python -m auction_hunter run          # kør i loop
.venv/bin/python -m auction_hunter web          # dashboard på :8080
```

`PYTHONPATH=src` er nødvendig fordi pakken ikke installeres, men køres fra
`src/`. Testene henter den selv via `pytest.ini`.

`scan` er den sikre start: den hverken gemmer i databasen eller sender til
Discord, så du kan se hvad profilen fanger, før du lader den løbe.

### Docker

```bash
cp .env.example .env        # udfyld DISCORD_WEBHOOK_URL
docker compose up -d --build
docker compose logs -f
```

Der startes to containere: agenten, som ikke har åbne porte, og dashboardet
på port 8080. Begge kører som ikke-root med read-only filsystem. Hukommelsen
og profilen ligger i `./data` og `./config` på værten, så de overlever
genopbygning.

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

Landsdelene og deres id'er står i `regions` i `config/interests.yml`. Standard
er `region_ids: all`, som følger alle fem:

```yaml
source:
  region_ids: all          # alle landsdele fra mappingen
  # region_ids:            # eller enkelte:
  #   - lyr0boj4d8         # Sjælland
  #   - epV0wO0K7l         # Fyn
```

Etiketten i beskederne udledes automatisk ("Hele Danmark" for alle) og kan
overstyres med `REGION_LABEL` eller `region_label`. Uden det ville varslerne
påstå "Sjælland" mens agenten fulgte hele landet. `REGION_IDS` i miljøet vinder
over YAML-filen.

### Profiler

En profil er et navngivet interessesæt med sine egne kategorier, sit eget
prisloft, sine egne udelukkelser og sin egen Discord-webhook:

```yaml
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
```

Uden `profiles` er der én implicit standardprofil, og alt kører som før.
`categories: null` betyder alle kategorier, `[]` betyder ingen. Globale
`exclude`-ord gælder altid. En profil slås fra med `enabled: false` eller
`PROFILE_<NØGLE>_ENABLED=0`. Dedup er pr. (lot, kategori, profil), så det samme
lot kan give én besked pr. profil, men aldrig to til den samme. Fund-siden kan
filtreres på profil, og `/drift` viser hver profil med webhook-status.

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

## Dashboard

Ud over Discord kører der et webdashboard, så fundene kan ses samlet i stedet
for at blive scrollet forbi i en chat. Det starter med `docker compose up` og
ligger på port 8080.

```bash
docker compose up -d          # scraper + dashboard
# eller uden Docker:
PYTHONPATH=src python -m auction_hunter web --port 8080
```

| Fane | Hvad den gør |
|---|---|
| **Fund** | Aktive fund som katalog, grupperet pr. dag, med pris og tid tilbage |
| **Udløbet** | Fund hvis auktion sluttede inden for 48 timer |
| **Mine** | Det du selv har markeret, med det samlede beløb for bud og køb |
| **Arkiv** | Fritekstsøgning i *alt* agenten har set, med auktion som filter |
| **Interesser** | Redigér profilen og test en titel mod reglerne |
| **Assistent** | Spørg om arkivet i almindeligt sprog |
| **Statistik** | Antal, støjandel og hvad fundene reelt koster |
| **Drift** | Kørsler, databasens og billedcachens størrelse, version, profiler og om hemmelighederne er sat |

Listen kan vises som katalog med billeder eller som kompakt liste, og
vælges med knappen i værktøjslinjen. Der er tastaturgenveje: `j` og `k` flytter
markeringen, `Enter` åbner lot'et, `x`, `b` og `c` markerer, og `?` viser
listen. Principperne bag udseendet, farverne og bevægelsen står i DESIGN.md. Alle
beløb i dashboardet og i Discord er den reelle pris inkl. salær og moms. Et
lot uden bud viser hvad første bud vil koste i stedet for 0 kr.

Til overvågning findes `/healthz` (liveness), `/readyz` (klarhed, kræver også
læsbar `interests.yml`) og `/metrics` i Prometheus-format med aggregerede tal.
`/metrics` er åbent som standard, så Prometheus kan skrape uden en
login-session; sæt `METRICS_TOKEN` for at kræve et bearer-token.

### Arkivet

Agenten gemmer hvert lot den ser, ikke kun dem der rammer profilen — omkring
2.200 pr. kørsel, hvoraf en håndfuld bliver til fund. Arkivfanen leder i dem
alle, så man kan slå op om noget har været til salg, selvom det aldrig udløste
en besked.

Søgningen bruger SQLites FTS5 med begge danske foldninger, så både
`hoejttaler` og `hojttaler` finder `højttaler`. Der kan filtreres på pris,
status, kategori, **auktion** (hvor lot'et kommer fra), hvornår det blev set, og
om det blev til et fund.

### Interesser

Profilen kan redigeres direkte fra siden: tilføj og fjern nøgleord på hvert
niveau, ret prisloft, og styr udelukkelser.

Ændringerne skrives **linje for linje** i `config/interests.yml`, så filens
kommentarer bevares. De bærer beslutningerne bag profilen — hvorfor `disk`
ikke er et nøgleord, hvorfor `server` ligger i `strong` — og et
PyYAML-gennemløb ville smide dem alle væk. Agenten genlæser filen ved hver
kørsel, så en ændring slår igennem inden for 15 minutter uden genstart.

Feltet **Test en titel** kører den rigtige matcher på en titel og viser hvilke
nøgleord der blev ramt, eller hvad der manglede. Det er den hurtigste vej til
at forstå hvorfor noget slap igennem eller blev væk.

Sektionen **Forslag fra dine markeringer** læser din feedback og foreslår
konkret at fjerne nøgleord der oftest fører til noget du afviser. Der skal
mindst fem afviste lots bag et ord, så et enkelt underligt lot ikke fører til
en ændring, og intet ændres uden et klik. Den viser også de lots du har budt
på eller købt, som profilen ikke fangede, som et hint om et manglende
nøgleord. Før du anvender et forslag, viser siden om det ville bryde et hårdt
`yes`/`no`-krav i facitlisten (`config/match_expectations.jsonl`), så en
ændring er en afvejning og ikke et gæt. Analysen er ren regelbaseret.

Knappen **Spørg modellen om manglende nøgleord** (`/interests?ai=1`) sender de
køb profilen ikke fangede til sprogmodellen og beder om et nøgleord pr. titel.
Den kører kun når du beder om det, så siden ikke spørger af sig selv. Python
tjekker at ordet faktisk står i titlen, at kategorien og niveauet findes, og at
ordet ikke allerede står der — et svar der ikke kan efterprøves, bliver ikke et
forslag.

### Assistent

Spørg i almindeligt sprog: *«har der været Sennheiser-forstærkere under 1.000
kr?»* eller *«vis pladespillere som ikke blev til fund»*.

Modellen skriver aldrig SQL. Den oversætter spørgsmålet til det samme
strukturerede filter som søgeformularen bruger, og Python bygger
forespørgslen. En prompt-injektion i en lot-titel kan derfor ikke nå
databasen — det værste der kan ske er en mærkelig søgning.

Assistenten bruger samme nøgle som AI-trinnet (`CLASSIFIER_API_KEY`). Uden
nøgle virker siden stadig, men som en almindelig nøgleordssøgning.

Modellen udvider spørgsmålet med beslægtede produkttyper og mærker i stedet for
kun at søge på de ord du selv skrev, og ordene lægges sammen med OR. Et
spørgsmål om «ting der normalt har SSD eller NVMe» bliver derfor til NAS,
mini-PC'er, servere og de øvrige produkttyper, ikke en ordret søgning efter
"nvme ssd". Den kan også vælge en kategori.

Finder søgeordene ingenting, slippes teksten og de strukturerede filtre
beholdes, så svaret ikke ender i ingenting. Under svaret er der et link til
arkivet med samme filter, hvor det kan rettes.

Svaret bygger også på tal fra arkivet selv: en median og et spænd over de
fundne lots, og hvad samme slags er gået for tidligere. Modellen finder ikke på
beløb, og hver række den får, bærer din egen markering, så den kan svare på
«har jeg afvist noget lignende».

Spørger du hvad en bestemt vare er værd, kan modellen pege på den kandidat
spørgsmålet handler om. Så hentes lot'ets egne sammenlignelige salg og dets
prisforløb, og de vises som et panel under svaret: typisk, laveste og højeste,
hvert tidligere salg med pris og dato, og pris-kurven.

Assistenten er en **samtale**. Hver tur gemmes i `data/conversations.db`, en fil
for sig, så webben aldrig skriver i agentens database. Modellen får kun de
sidste seks beskeder og det forrige filter, så prisen pr. tur ikke vokser med
samtalens længde. **Ny samtale** starter forfra, og gamle samtaler ryddes efter
90 dage.

### Billeder

Auktionshuset fjerner et lots billede i samme øjeblik auktionen lukker. Det er
præcis der billedet er mest værd — Udløbet-fanen er hvor man skal genkende hvad
man overvejede at byde på.

Agenten henter derfor miniaturen mens lot'et stadig er aktivt, og kun for de
lots der er blevet til et fund eller ligger til gennemsyn. De øvrige ~2.200
hentes aldrig. Billederne ligger i `data/images/` ved siden af databasen, fylder
omkring 30 KB pr. fund, og ryddes automatisk når lot'et har været afsluttet i
180 dage.

Alt ved det er fail-open: kan billedet ikke hentes, vises pladsholderen som før,
og en død billedserver kan hverken vælte en kørsel eller en side. Slå det fra
med `CACHE_IMAGES=0`.

### Lot-siderne

Lot-listen indeholder kun en titel, og en titel kan ikke sige om varen er i
stykker. Med details.enabled: true i config/interests.yml henter agenten også
selve lot-siden for hvert **fund** og leder efter danske vendinger som
"defekt", "reserverede", "ubrugt" og "afhentning". Resultatet står på lot'ets
side og som et lille mærke på kortet, og Discord-beskeden får et Stand-felt.

Udtrækket er strukturuafhængigt: det læser sidens brødtekst frem for at gå efter
bestemte CSS-klasser, som ville fejle tavst den dag siden ændrer sig.

Det er **slået fra som standard**, fordi det er et ekstra kald til
auktionshuset for hvert fund, og deres vilkår kun tillader ét katalog-scrape
hvert 15. minut. Slår du det til, så hold max_per_run lav.

### Lot'ets side

Klik på "historik" på et kort for at se lot'ets prisforløb gennem de
observationer agenten har, og hvad **samme slags lot** er gået for tidligere.
Sammenligningen vægter ord efter hvor sjældne de er i arkivet, så "Sennheiser
HD 650" rangerer de andre HD 650'er øverst frem for alle Sennheiser-lots.
Beløbene er inkl. salær og moms, altså til at sammenligne med prisen på kortet.

### Markering til AI-træning

Hvert fund har fire knapper: **Afvis**, **Følg**, **Budt** og **Købt**. De
gemmes i tabellen `feedback` som træningsdata — et menneskeligt svar på om
nøgleordene og AI-trinnet ramte rigtigt. Klik igen for at fortryde.

Bud og køb sker sjældent, men Udløbet-fanen gør det overkommeligt: der står
kun det der er afgjort for nylig, så en dags fund kan markeres ad gangen. Alt
du har markeret samles under **Mine**, med det samlede beløb for bud og køb.

Statistikfanen viser tre ting: **støjandelen** pr. kategori, altså hvor stor en
del af dens fund du har afvist (en høj andel betyder at nøgleordene er for
brede), hvor mange fund der ligger i hvert prisleje, og hvad fundene i
gennemsnit koster. Markeringerne kan hentes som CSV fra sidefoden, og hele arkivet med priser og datoer som arkiv.csv til videre analyse i et regneark.

### Adgangskode

Dashboardet kan redigere interesseprofilen, så det bør ikke stå åbent — heller
ikke på et hjemmenetværk. Sæt `WEB_PASSWORD` i `.env`:

```bash
WEB_PASSWORD=vælg-noget-langt-her
# Så sessioner overlever en genstart:
WEB_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
```

Uden `WEB_PASSWORD` kører siden åbent, og det logges som en advarsel ved
opstart. Adgangskoden kan også læses fra en fil eller en anden variabel,
som de øvrige hemmeligheder.

Bag en reverse proxy eller på et domæne er der fire variabler mere:

| Variabel | Gør |
|---|---|
| `WEB_BASE_URL` | Sætter login-cookien til `Secure` når den er `https://` |
| `ALLOWED_HOSTS` | Afviser andre `Host`-headere (DNS-rebinding) |
| `FORWARDED_ALLOW_IPS` | Hvilke proxyer der må sætte `X-Forwarded-*` |
| `METRICS_TOKEN` | Kræver bearer-token på `/metrics` |

Usikre metoder afvises desuden, hvis `Origin`/`Referer` peger på en anden vært,
så `fetch`-kaldet til `/feedback` også er dækket.

Siden sætter `noindex,nofollow` og er ikke tænkt til at ligge på internettet.
At genudgive auktionshusets data offentligt er noget andet end at scrape til
eget brug.

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
.venv/bin/pip install pytest "httpx2>=2.13"   # kun til udvikling
.venv/bin/python -m pytest
```

To slags tests:

- **Enhedstests** i `tests/test_textmatch.py` og `tests/test_matcher.py` bruger
  syntetiske titler og tester reglerne isoleret.
- **Facitlisten** i `config/match_expectations.jsonl` indeholder rigtige
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
| `images.py` | Lokal cache af lot-billeder |
| `web/` | Webdashboard: faner, søgning, redigering, assistent |
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

## Backup og gendannelse

```bash
.venv/bin/python -m auction_hunter backup --out backups
.venv/bin/python -m auction_hunter restore backups/hunter-<tidsstempel>.tar.gz --yes
```

Backup er én `tar.gz` med alt der ikke ligger i git:

```
db/main.db            agentens database
db/conversations.db   samtalernes database, hvis den findes
images/               de cachede miniaturebilleder, hvis de findes
```

Databaserne tages med SQLites `VACUUM INTO`, som giver et konsistent
øjebliksbillede mens agenten skriver, og som giver rene filer uden
WAL-søskende. Et snapshot der fejler integritetstjekket bliver ikke skrevet.
Gendannelse kræver at `web` og `hunter` er stoppet, sikrer alle tre dele
først, og skriver dem på plads i ét flyt. `config/interests.yml` er bevidst
ikke med: den ligger i git, og en gendannelse skal ikke kunne rulle
profilændringer tilbage. Se `DEPLOYMENT.md` for detaljer.

## Licens

MIT. Se `LICENSE`.

Kort fortalt: du må bruge, ændre og videredistribuere koden, også
kommercielt, så længe copyright-linjen følger med. Softwaren leveres uden
garanti.

