# Designsprog

Dashboardets visuelle system. Kort fortalt hvorfor tingene ser ud som de gør, og
hvilke tal der ikke må ændres uden at regne efter.

Filerne er `web/static/app.css` (alle tokens), `web/templates/_icons.html`
(ikoner) og `web/static/app.js` (bevægelse og tilstande). Der er intet
byggetrin, ingen CSS-framework og ingen eksterne kald.

## Typografi

| Rolle | Skrift | Størrelse | Line-height | Tracking | Vægt |
|---|---|---|---|---|---|
| Display | Bricolage Grotesque | 40 px | 1.08 | -0.02em | 700 |
| h1 | Bricolage Grotesque | 28 px | 1.15 | -0.015em | 600 |
| h2 | Bricolage Grotesque | 23 px | 1.22 | -0.01em | 600 |
| h3 / sektion | Bricolage Grotesque | 19 px | 1.3 | -0.005em | 600 |
| Brødtekst | Onest | 15 px | 1.55 | 0 | 400 |
| Titel i kort | Onest | 15 px | 1.35 | -0.005em | 600 |
| Pris | Onest | 17 px | 1.2 | -0.02em | 650 |
| Meta | Onest | 12 px | 1.35 | 0 | 500 |
| Overline | Onest | 11 px | 1.1 | +0.08em | 600 |

Skalaen er 1.2 fra 16 px. Bricolage bruges **kun** over 24 px: under det bliver
dens detaljer urolige i tæt tekst. Begge fonte er variable og selvhostede i
`static/fonts/` (ca. 100 KB i alt for latin og latin-ext).

**Hvorfor ikke Plus Jakarta Sans.** Den blev målt og forkastet: dens mellemrum
er 0,17 em mod Onests 0,27 em. Ved 12 px fik det "Slutter om 1 dag 8 t" til at
læse som "Slutterom 1 dag 8 t". Målingen ligger i commit-historikken, og
tommelfingerreglen er: et UI-mellemrum under 0,22 em ser sammenklemt ud i små
størrelser.

**Tal.** `font-variant-numeric: tabular-nums lining-nums` sættes på
`.num, .price, .metric dd, .result-count, .meter-value, .cat-meta`. Husk at
egenskaben arver: sæt den på det element der viser tal, ikke på en forfader til
løbende tekst.

## Farve

Alle ramper er bygget i OKLCH og konverteret til sRGB. Lys tilstand er standard.

| Rolle | Lys | Mørk |
|---|---|---|
| Sidebaggrund | `#f8f4ef` | `#0e0c09` |
| Flade (kort, panel) | `#fefcfa` | `#161310` |
| Hævet / hover | `#f3eee8` | `#1c1915` |
| Kant | `#e6e0d7` | `#342f2a` |
| Kant, stærk | `#d1c9c0` | `#4d4740` |
| Kant på input | `#948d85` | `#746d66` |
| Tekst | `#36322d` | `#ebe7e2` |
| Tekst, sekundær | `#57524b` | `#cec9c3` |
| Tekst, dæmpet | `#777169` | `#b0aaa3` |
| Kun ikoner og streger | `#948d85` | `#918b84` |
| Accent (tekst, link) | `#a05c16` | `#f5a259` |
| Accent, brandflade | `#e8954a` | `#e8954b` |
| Tekst på accent | `#fffdfa` | `#1a1611` |

Neutralens hue er 74 (varm), og chroma holdes under 0,016: varmen skal komme fra
tonen, ikke fra mætningen, ellers bliver fladerne cremede.

**Fire tekstniveauer, alle over 4,5:1.** `--text` 12:1, `--text-2` 7,4:1,
`--text-3` 4,6:1 og `--text-4` 3,1:1. Det sidste må **kun** bruges til ikoner
og streger, aldrig til tekst. Bryder man den regel, falder meta-linjer og
hjælpetekster under WCAG AA.

**Accent og knapper.** `#e8954a` med hvid tekst giver 2,3:1 og må ikke bruges
som knap i lys tilstand. Den lyse primærknap bruger derfor `--accent`
(`#a05c16`, hvid tekst 5,2:1) og hover `--accent-deep`. I mørk tilstand er det
omvendt: den lyse amber med mørkt blæk.

**Kategorifarver.** Okabe-Ito-farver, som er konstrueret til at kunne skelnes
ved rød-grøn-blindhed. Hver kategori har to varianter: `--cat-color` til prikken
(3:1 som grafisk objekt) og `--cat-text` til etiketten (4,5:1 som tekst).
Farven er altid redundant: etiketten står ved siden af, så farveblinde mister
ingen information.

| Kategori | Prik | Tekst |
|---|---|---|
| it_tech | `#0072b2` | `#2377b2` |
| audio_hifi | `#009e73` | `#00835f` |
| watches | `#be861d` | `#996800` |
| maker_electronics | `#249ad3` | `#007bad` |
| gaming | `#ce72a6` | `#a95886` |
| coffee | `#d55e00` | `#ae5d31` |

## Rum, form og dybde

- **Spacing**: 4 px-grundlag, trinene 2/4/6/8/12/16/24/32/48/64/96. Tre
  tilstande: tæt (4/8) i tabeller og chips, standard (12/16) i kort og felter,
  rundhåndet (32/48/64) mellem sektioner. Forholdet mellem to gruppeniveauer skal
  være mindst 2x, ellers ser alt lige vigtigt ud.
- **Radius efter størrelse**: 3 px til badges, 5 px til knapper og felter, 8 px
  til kort og miniaturer, 12 px til paneler, pille til chips og søgefeltet.
  Uniform radius ser billigt ud, fordi forholdet mellem kurve og flade bliver
  forkert.
- **Bredder**: 1020 px til lister, 860 px til læse- og editorflader
  (`is-narrow`), 1180 px til tabeller (`is-wide`). Én bredde fejler i begge
  retninger: for bred til at læse, for smal til en tabel.
- **Dybde**: `--e1` hvilende kort, `--e2` hævet og hover, `--e3` overlay,
  `--e4` modal. Højst to niveauer synligt på en side. Skyggen er varm
  (`rgba(50,38,22,…)`), ikke sort. I mørk tilstand kommer dybden fra lysere
  flader, og skyggerne er kun til svævende lag.
- **Kanter**: kun hvor kanten bærer betydning (input, fokus, valgt, aktiv).
  Ellers skaber en baggrundsforskel og en skygge laget. Det er dét der fjerner
  "alt er en boks".

## Bevægelse

| Token | Værdi | Bruges til |
|---|---|---|
| `--dur-instant` | 80 ms | tryk |
| `--dur-fast` | 140 ms | hover, fokus, farve og kant |
| `--dur-base` | 220 ms | tilstandsskift, details, filtrering |
| `--dur-slow` | 320 ms | kort der indgår |
| `--dur-slower` | 480 ms | første maling |
| `--stagger` | 35 ms | forskydning, maks 9 elementer |

Kurver: `--ease-standard` `cubic-bezier(.2,0,0,1)` til farve og kant,
`--ease-out` `cubic-bezier(.22,1,.36,1)` til alt der kommer ind,
`--ease-in` til det der forsvinder, `--ease-expressive` til enkelte pops.
Ingen bounce.

Der animeres **kun** `transform` og `opacity`. Aldrig `transition: all`,
aldrig højde eller bredde. Kort får en forskudt indgang via `--i`, sat i
skabelonen og begrænset til 9. `prefers-reduced-motion` nulstiller varigheder
og forskydning, men beholder farveskift.

## Regler der ikke må brydes

1. Al tekst er mindst `--text-3`. `--text-4` er til ikoner og streger.
2. Primærknappen i lys tilstand bruger `--accent` med `--on-accent`, aldrig
   brand-amberen med hvid tekst.
3. Kategori-farve er altid redundant; etiketten skal kunne læses uden farve.
4. Ingen emoji i brugerfladen; ikoner er inline SVG med 1,7 px streg.
5. Ingen inline JavaScript, for CSP'en tillader kun script fra `/static`.
6. Nye farver skal kontrastberegnes mod den faktiske baggrund, ikke mod hvid.
