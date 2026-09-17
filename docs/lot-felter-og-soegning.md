# Lot-felter og søgning

Research af hvad auktionshuset viser om et lot, hvad agenten gemmer i dag, og
hvilke filtre det kan blive til. Skrevet efter at lot-siden, kataloget og
auktionslisten blev læst igennem mod den rigtige side (september 2026).

## Hvad siden viser

### Lot-siden

- **Lot nr., titel og beskrivelse.** Beskrivelsen ligger i en prosa-blok og
  gentages i `meta name="description"`. Den er allerede udtrukket af
  `details.py`, men kun når `details.enabled` er slået til.
- **Kategori.** Auktionshusets egen kategori står som forled i titlen
  ("Lydudstyr: Behringer …", "IT / tech / hacking: …"). Den er ikke den samme
  som interesseprofilens kategorier.
- **Status og tid.** Aktiv eller afsluttet, hammerslagstidspunkt og nedtælling.
- **Priser.** Højeste bud og total inkl. salær og moms.
- **Auktionsinfo** (auktions-niveau, gentages på hvert lot):
  - Auktionsadresse (gade og `DK-postnr by`)
  - Auktionen slutter
  - Eftersyn (kan være "Ingen")
  - Udlevering
  - Sælges for (sælger)
  - Kontakt (telefon og mail)
  - **Levering:** Forsendelse (tilgængelig/ikke tilgængelig), Gaffeltruck
    (til rådighed/ikke) og Palleløfter (til rådighed/ikke)
- **Budhistorik** og **auktionsbetingelser** (foldes ud).
- **Forrige/næste lot.**

### Kataloget

Katalogsiden (`/auktioner/<slug>?page=N`), som agenten allerede henter for at
få lot'ene, indeholder **hele auktionsinfo-panelet** ovenfor. Levering, adresse,
eftersyn, udlevering og sælger kan altså læses uden ét ekstra kald.

Lot-listen på katalogsiden giver desuden titel, lot nr., bud, total, sluttid,
billede og om der er bud.

### Auktionslisten

- **Landsdele** bruges kun som filter: Nordjylland, Midtjylland, Sønderjylland,
  Fyn og Sjælland. Regionen står **ikke** på det enkelte auktionskort, og den
  kan derfor ikke læses ud af kataloget eller lot-siden.
- **Auktionstype** står derimod på kortet: Konkursauktion, Overskudsauktion,
  Ophørsauktion, Flytteauktion eller Dødsbo. `Scraper._auction_type` læser den,
  og den gemmes nu pr. lot.
- Auktionsadresse, antal lots og sluttidspunkt.

## Hvad agenten gemmer

`lots` har `lot_id, auction_id, auction_title, title, url, lot_number,
first_seen, last_seen, first_bid, last_bid, last_total, ends_at, image_url`,
`details, details_flags, details_at` og nu `region, auction_type, address,
shipping`. De fire sidste er auktions-niveau og hentes fra auktionsinfo-panelet
i kataloget, som allerede hentes for at få lot'ene.

Søgningen (`SearchQuery`) kan filtrere på tekst, pris, status, fund/ikke-fund,
kategori, auktionstitel, landsdel, auktionstype, "kan sendes", dage tilbage og
sortering.

## Tilføjet

1. **Levering (Forsendelse).** `shipping` pr. lot, et grønt "kan sendes"-mærke
   på kortet og et filter i arkivet. Det er dét der gør et lot i den anden ende
   af landet relevant.
2. **Auktionsadresse og auktionstype.** Vises på lot-siden under "Om lot'et";
   auktionstypen kan filtreres.
3. **Landsdel.** Hentes ved at kalde auktionslisten én gang pr. landdel og
   mærke hver auktion med det kald. Vises på kort og lot-side og kan filtreres.

## Tilbage

- **Eftersyn og udlevering** står i panelet og kan vises på lot-siden.
- **Gaffeltruck og Palleløfter** læses ikke; kun Forsendelse gemmes.
- **Auktionshusets egen kategori** (forleddet i titlen, fx "Lydudstyr") bruges
  ikke; kortet viser interesseprofilens kategori.

## Noter og risici

- Alt auktionsinfo er **auktions-niveau**, ikke lot-niveau. Det passer med at
  `auction_title` allerede gemmes pr. lot; kolonnerne bliver bredere, ikke
  skæve.
- Nye kolonner kræver en migration i `storage.py`, og webben skal læse dem
  gennem `has_column`, så containeren kan starte før agenten har migreret.
- Leveringsteksten skal parses tolerant: den synlige tekst siger "Truck" mens
  `title`-attributten siger "Gaffeltruck", og "Forsendelse" har ingen title.
  Den synlige tekst er den stabile: "Forsendelse tilgængelig" /
  "Forsendelse ikke tilgængelig".
- Flere auktionslistekald pr. kørsel er stadig ét kald pr. interval, men
  `runs.requests` og /drift skal vise det, så det kan efterprøves.
