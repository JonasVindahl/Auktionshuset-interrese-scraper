# Designsprog

Dashboardets visuelle system. Kort fortalt hvorfor tingene ser ud som de gør, og
hvilke tal der ikke må ændres uden at regne efter.

Filerne er web/static/app.css (alle tokens), web/templates/_icons.html (ikoner),
web/templates/_empty.html (tomme tilstande) og web/static/app.js (bevægelse og
tilstande). Der er intet byggetrin, ingen CSS-framework og ingen eksterne kald.

## Retning: varm auktionskatalog

Fladen skal læse som et trykt auktionskatalog, ikke som et admin-panel. Det
betyder fire ting, og de hænger sammen:

- **Papir i bunden, hvidt ovenpå.** Sidebaggrunden er en dybere varm sand, og
  kortene er næsten hvide. Forskellen er det der giver figurgrund. Uden den
  smelter alt sammen til ét beige felt, og det var netop dét der føltes klinisk.
- **Amber er en rigtig farve.** Brandfarven bruges som flade: den 3 px tykke
  streg i toppen af rammen, segmentet under sidehovedet, prikker, ikoner og
  løftede tal. Som tekst og knap bruges den mørke accent, som har kontrasten.
- **Store bogstaver, store tal.** Display og h1 er skruet op, og prisen er det
  største tal på flisen. Hierarkiet skal kunne læses på fem sekunder.
- **Karakter i rammen, ro i data.** Personligheden ligger i topbar, sidehoved,
  tomme tilstande og de dataløse flader. Priser, tid og tabeller er stille og
  forudsigelige.

## Typografi

| Rolle | Skrift | Størrelse | Line-height | Tracking | Vægt |
|---|---|---|---|---|---|
| Display / hero | Bricolage Grotesque | 44 px | 1.06 | -0.025em | 700 |
| h1 | Bricolage Grotesque | 34 px | 1.06 | -0.025em | 700 |
| h2 | Bricolage Grotesque | 24 px | 1.22 | -0.01em | 600 |
| h3 / sektion | Bricolage Grotesque | 19 px | 1.3 | -0.005em | 600 |
| Pris | Onest | 23 px | 1.1 | -0.03em | 750 |
| Titel i kort | Onest | 16 px | 1.32 | -0.01em | 600 |
| Brødtekst | Onest | 15 px | 1.55 | 0 | 400 |
| Meta | Onest | 12 px | 1.35 | 0 | 500 |
| Overline | Onest | 11 px | 1.1 | +0.14em | 700 |

Skalaen er 1.2 fra 16 px. Bricolage bruges fra 24 px og opefter: under det
bliver dens detaljer urolige i tæt tekst. Begge fonte er variable og selvhostede
i static/fonts/ (ca. 100 KB i alt for latin og latin-ext).

**Hvorfor ikke Plus Jakarta Sans.** Den blev målt og forkastet: dens mellemrum
er 0,17 em mod Onests 0,27 em. Ved 12 px fik det "Slutter om 1 dag 8 t" til at
læse som "Slutterom 1 dag 8 t".

**Tal.** font-variant-numeric: tabular-nums lining-nums sættes på .num, .price,
.metric dd, .result-count, .meter-value, .lot-price-value og .cat-meta. Husk at
egenskaben arver: sæt den på det element der viser tal.

## Farve

Alle ramper er bygget i OKLCH og konverteret til sRGB. Lys tilstand er standard.

| Rolle | Lys | Mørk |
|---|---|---|
| Sidebaggrund | #ece3d5 | #14110d |
| Flade (kort, panel) | #fffdfa | #1e1a15 |
| Hævet / hover | #f5efe4 | #26211a |
| Sænket / spor | #e7ddce | #2f2921 |
| Kant | #ded3c2 | #3a332a |
| Kant, stærk | #bfb2a0 | #554c40 |
| Kant på input | #877e6f | #7c7466 |
| Tekst | #211c16 | #f0eae1 |
| Tekst, sekundær | #4a433a | #d4cdc2 |
| Tekst, dæmpet | #6a6257 | #b3aba0 |
| Kun ikoner og streger | #948b7d | #948c80 |
| Accent (tekst, link) | #8f4c0f | #f3a55e |
| Accent, brandflade | #e8954a | #e8954b |
| Tekst på accent | #fffdfa | #1a140d |

Neutralens hue er 74 (varm), og chroma holdes under 0,016: varmen skal komme fra
tonen, ikke fra mætningen. Body bærer en svag varm glød
(radial-gradient med accent-solid ved 14 % mod transparent) over sandfarven, så
toppen af siden får lys og ikke er et fladt felt.

**Fire tekstniveauer, alle over 4,5:1.** --text ca. 15:1, --text-2 ca. 8:1,
--text-3 5,7:1 på surface og 4,7:1 på sidebaggrunden, --text-4 3,2:1. Det sidste
må **kun** bruges til ikoner og streger, aldrig til tekst.

**Accent og knapper.** Brand-amberen (#e8954a) med hvid tekst giver 2,3:1 og må
ikke bruges til tekst eller knap i lys tilstand. Den bruges derfor som flade
(topstreg, prikker, glyffer, måler) og som brand-tile. Links og primærknappen
bruger --accent (#8f4c0f, hvid tekst 6,5:1) og hover --accent-deep. I mørk
tilstand er det omvendt: den lyse amber med mørkt blæk.

**Kategorifarver.** Okabe-Ito-farver, som kan skelnes ved rød-grøn-blindhed. Hver
kategori har --cat-color til prikken og --cat-text til etiketten. Farven er altid
redundant: etiketten står ved siden af, så farveblinde mister ingen information.

| Kategori | Prik | Tekst |
|---|---|---|
| it_tech | #0072b2 | #2e6bb3 |
| audio_hifi | #009e73 | #007771 |
| watches | #be861d | #8c6200 |
| maker_electronics | #249ad3 | #0070a6 |
| gaming | #ce72a6 | #a24b7e |
| coffee | #d55e00 | #b84300 |

## Rum, form og dybde

- **Spacing**: 4 px-grundlag, trinene 2/4/6/8/12/16/24/32/48/64/96. Tre
  tilstande: tæt (4/8) i tabeller og chips, standard (12/16) i kort og felter,
  rundhåndet (32/48/64) mellem sektioner. Forholdet mellem to gruppeniveauer skal
  være mindst 2x, ellers ser alt lige vigtigt ud.
- **Radius efter størrelse**: 3 px til badges, 5 px til knapper og felter, 8 px
  til kort og miniaturer, 12 px til paneler, pille til chips og søgefeltet.
  Uniform radius ser billigt ud, fordi forholdet mellem kurve og flade bliver
  forkert.
- **Bredder**: 1240 px til kataloger og tabeller (is-wide), 1020 px til lister,
  860 px til læse- og editorflader (is-narrow). Topbarens indre kolonne
  (.topbar-inner) og sidefoden følger samme 1240 px-kolonne, så rammen flugter
  med indholdet i stedet for at stå ude ved kanten.
- **Dybde**: --e1 hvilende kort, --e2 hævet og hover, --e3 overlay, --e4 modal.
  Højst to niveauer synligt på en side. Skyggen er varm (rgba(50,38,22,...)),
  ikke sort. I mørk tilstand kommer dybden fra lysere flader.
- **Kanter**: kun hvor kanten bærer betydning (input, fokus, valgt, aktiv).
  Ellers skaber en baggrundsforskel og en skygge laget.

## Bevægelse

| Token | Værdi | Bruges til |
|---|---|---|
| --dur-instant | 80 ms | tryk |
| --dur-fast | 140 ms | hover, fokus, farve og kant |
| --dur-base | 220 ms | tilstandsskift, details, filtrering |
| --dur-slow | 320 ms | kort der indgår |
| --dur-slower | 480 ms | første maling |
| --stagger | 35 ms | forskydning, maks 9 elementer |

Kurver: --ease-standard til farve og kant, --ease-out til alt der kommer ind,
--ease-in til det der forsvinder, --ease-expressive til enkelte pops. Ingen
bounce. Der animeres **kun** transform og opacity. prefers-reduced-motion
nulstiller varigheder og forskydning, men beholder farveskift.

## Komponenter

- **Topbar.** Fuld bredde med indre kolonne på 1240 px. 3 px amber-streg i toppen
  (inset box-shadow, så højden ikke flytter sig), varm gradient, og brand-tile i
  dyb accent-gradient. Aktiv fane er en amber-soft pille med accent-kant.
  Under 760 px: brand og status på første linje, navigationen wrapper på anden.
- **Sidehoved.** Overline (versaler, amber, +0.14em) over en Bricolage-h1, en
  meta-linje og en 1 px kant med et 58 px amber-segment. Hver side har en
  overline, så rytmen er den samme hele vejen igennem.
- **Kort.** Billedplade i 3:2. Uden billede vises en varm plade med skrå
  skravering og en amber-glyf, så et manglende billede læser som en bevidst
  flade og ikke som en fejl. Tidspille nederst til venstre, statusstempel øverst
  til højre, pris i 23 px tabular, titel i op til to linjer, metarække og
  markeringsrække.
- **Markering.** Fire handlinger pr. kort. På flisen vises kun teksten (Afvis,
  Følg, Budt, Købt), fordi et ikon ved siden af ville klemme etiketten til "...".
  På lot-siden er der plads til ikon plus tekst i en kantet knap. Aktiv
  markering bruger statusfarverne: rød (afvist), blå (følger), amber (budt),
  grøn (købt).
- **Kompakt liste.** Én kolonne: 44 px miniature, pris og tid øverst, titel,
  metarække og handlinger med ikon og tekst. Ni rækker mere pr. skærm end
  kataloget.
- **Tomme tilstande.** .empty er en stiplet, dæmpet boks til små tilfælde.
  .empty-state er den fulde: glyf i en amber-cirkel, Bricolage-overskrift,
  forklarende tekst og et kald til handling. En tom skærm skal altid forklare
  hvor man er, og hvad man gør nu.
- **Et enkelt lot.** Prisen er helten: et panel med 3 px amber-topkant, "Reel
  pris" som overline, beløbet i 38 px, og bud plus hammerslag som underrække.
  Derunder prisforløb og "Om lot'et". Lignende salg står i højre kolonne.
- **Statistik.** Metrikfliser med tal i 36 px/750. Bjælker i 12 px højde med
  kategorifarve, og støjandelen som en lille måler ved siden af.

## Regler der ikke må brydes

1. Al tekst er mindst --text-3. --text-4 er til ikoner og streger.
2. Brand-amberen (#e8954a) bruges som flade, aldrig som tekst eller knap i lys
   tilstand. Links og primærknap bruger --accent med --on-accent.
3. Kategori-farve er altid redundant; etiketten skal kunne læses uden farve.
4. Ingen emoji i brugerfladen; ikoner er inline SVG med 1,7 px streg.
5. Ingen inline JavaScript, for CSP'en tillader kun script fra /static.
6. Nye farver skal kontrastberegnes mod den faktiske baggrund, ikke mod hvid.
7. Hver side har en overline over h1, og hver sidehoved-kant har præcis ét
   amber-segment.
8. Markeringerne på en flise har altid deres synlige tekst. Bliver de klemt, skal
   layoutet skifte (fx to rækker på mobil), ikke etiketten forkortes.
9. En tom tilstand uden glyf, overskrift og vej videre er en fejl.
