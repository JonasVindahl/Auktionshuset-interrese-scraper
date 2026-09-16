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

- `strong` — konkrete produkttyper. `højttaler`, `espressomaskine`, `proxmark`.
- `weak` — brede ord. `server` (møblet, servering), `stereo`, `rack`. Kræver to
  **forskellige** træffere; varianter som `højtaler`/`højtalere` tælles som ét.
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
- **Indrykning i `interests.yml` er 2 mellemrum for kategori, 4 for niveau, 6
  for nøgleord.** Et nøgleord på forkert niveau læses som et andet felt.
- **`git status` i dette miljø er upålideligt** og kan melde "clean" efter en
  redigering. Verificér med `grep`/`sed` eller `git diff`, ikke med `git status`.
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
