"""Chat-assistent over arkivet og interesseprofilen.

To ting den kan:

**Søge i arkivet med almindeligt sprog.** Modellen skriver aldrig SQL. Den
oversætter spørgsmålet til et struktureret filter (samme felter som
søgeformularen), og Python bygger forespørgslen. Dermed kan et prompt-angreb
i en lot-titel ikke nå databasen — det værste der kan ske er en mærkelig
søgning.

**Forklare hvorfor et lot matchede eller ikke matchede.** Den del kræver ingen
model: matcheren ved det allerede. Vi kører den rigtige matcher på titlen og
viser hvilke nøgleord der blev ramt, og hvad der manglede. Modellen bruges kun
til at formulere svaret og foreslå ændringer.

Fejler modellen, svarer chatten stadig — med rå søgeresultater i stedet for
prosa. Det følger samme fail-open-princip som klassificeringen: et teknisk
problem må ikke gøre værktøjet ubrugeligt.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode

from ..classifier import LLMError, OpenAICompatibleClient
from ..config import Config
from ..matcher import match_lot
from ..scraper import Lot
from ..textmatch import find_keywords
from . import queries, similar
from .formatting import kr, time_left
from .search import SearchQuery, search

log = logging.getLogger(__name__)

MAX_QUESTION_LENGTH = 500
# Hvor mange kandidater modellen må se på. Søgeresultatet er allerede begrænset
# til en side, og hver række koster omkring 22 tokens, så 40 rækker er under
# 900 tokens — det er ikke her omkostningen ligger.
MAX_CANDIDATES = 40

FILTER_SYSTEM = (
    "Du oversætter et dansk spørgsmål om et auktionsarkiv til et JSON-filter. "
    "Svar KUN med JSON, ingen forklaring.\n\n"
    "Felter (alle valgfri):\n"
    "  soegeord    liste af ENKELTE ord, ikke sætninger, og uden citationstegn. "
    "Ordene lægges sammen med OR, så ét ord er nok. Udvid spørgsmålet med de "
    "produkttyper og mærker der ville svare på det, ikke kun de ord brugeren "
    "selv skrev. Spørger nogen efter 'ting der normalt har SSD eller NVMe', så "
    "medtag også nas, nuc, minipc, server, laptop, synology og lignende.\n"
    "  alle_ord    næsten altid false. Sæt kun true når hele spørgsmålet er ét "
    "bestemt modelnummer, fx 'sennheiser hd650', ellers giver AND et tomt svar.\n"
    '  liste       "stigere" naar brugeren spoerger hvad der er steget mest i '
    "pris, eller hvilke bud der er loebet op. Ellers udelades feltet.\n"
    "  min_price   heltal, kroner\n"
    "  max_price   heltal, kroner\n"
    '  status      "alle" | "aktive" | "afsluttede"\n'
    '  matched     "alle" | "kun_fund" | "kun_ikke_fund"\n'
    "  kategori    en af kategorinøglerne. Udelad den naar brugeren spoerger "
    "om noget der ikke blev et fund, for et ikke-fund har ingen kategori.\n"
    "  days_back   heltal, hvor mange dage tilbage\n"
    '  sort        "relevans" | "nyeste" | "slutter" | "pris_op" | "pris_ned"\n\n'
    'Brug matched="kun_ikke_fund" når brugeren spørger om noget der IKKE blev '
    "fanget af profilen. Brug status=\"aktive\" når de spørger om noget de kan "
    "byde på nu."
)

# Modellen vælger selv hvilke af kandidaterne der er svar på spørgsmålet.
# Søgningen er grov med vilje — den skal hellere give for meget end for lidt —
# og modellen er det led der kan se om et lot reelt besvarer spørgsmålet.
ANSWER_SYSTEM = (
    "Du er en hjælpsom assistent for en dansk auktionsagent. "
    "Du får et spørgsmål og en nummereret liste af lots fra databasen. "
    "Svar KUN med JSON: {\"valgte\": [1, 4], \"svar\": \"kort tekst på dansk\"}.\n\n"
    "valgte   numrene på de lots der reelt besvarer spørgsmålet, i den "
    "rækkefølge de skal vises. Vælg kun dem der er et svar; er ingen "
    "relevante, så brug en tom liste.\n"
    "svar     højst tre sætninger. Nævn priser og hvornår ting slutter når det "
    "er relevant. Find ikke på lots der ikke står i listen, og find ikke på "
    "beløb. Hvis ingen af lot'ene besvarer spørgsmålet, så sig det direkte i "
    "stedet for at vælge det nærmeste. Du ser kun den liste du har fået, så "
    "påstå aldrig at noget slet ikke findes i arkivet. Du får også et "
    "prisoverblik over det fundne sæt og, når det findes, hvad samme slags er "
    "gået for tidligere; brug de tal hvis spørgsmålet handler om hvad noget er "
    "værd.\n"
    "sammenlign  valgfrit. Nummeret på den kandidat spørgsmålet handler om, når "
    "det handler om hvad noget er værd. Så hentes arkivets tidligere salg af "
    "samme slags og lot'ets eget prisforløb og vises under svaret. Udelad "
    "feltet hvis spørgsmålet ikke handler om en bestemt vare."
)

_FEEDBACK_LABELS = {
    "skip": "du har afvist",
    "watch": "du følger",
    "bid": "du har budt",
    "bought": "du har købt",
}

SUGGEST_SYSTEM = (
    "Du hjælper med at forbedre en dansk auktionsagents nøgleordsprofil. "
    "Du får titler som brugeren selv har budt på eller købt, men som agenten "
    "ikke fangede. Foreslå ét nøgleord pr. titel der ville have fanget den. "
    "Svar KUN med JSON og intet andet."
)


SUGGEST_SYSTEM = (
    "Du hjælper med at forbedre en dansk auktionsagents nøgleordsprofil. "
    "Du får titler som brugeren selv har budt på eller købt, men som agenten "
    "ikke fangede. Foreslå ét nøgleord pr. titel der ville have fanget den. "
    "Svar KUN med JSON og intet andet."
)


EXPLAIN_SYSTEM = (
    "Du hjælper med at finjustere en dansk auktionsagents nøgleordsprofil. "
    "Du får en lot-titel og en analyse af hvorfor den matchede eller ikke "
    "matchede. Forklar kort hvorfor, og foreslå konkret hvilket nøgleord der "
    "skal tilføjes eller udelukkes, og på hvilket niveau "
    "(strong/weak/brands/exact). Svar på dansk, maks. 5 linjer."
)


@dataclass
class Valuation:
    """Et lot assistenten vurderer: dets egne tal og hvad samme slags gik for."""

    lot_id: str
    title: str
    url: str
    total: int | None = None
    comps: object = None
    series: list = field(default_factory=list)


@dataclass
class ChatAnswer:
    text: str
    rows: list[sqlite3.Row] = field(default_factory=list)
    filter_used: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False      # True når modellen ikke kunne nås
    considered: int = 0         # hvor mange kandidater modellen fik at vælge fra
    selected: bool = False      # True når modellen selv valgte rækkerne
    total: int = 0              # hvor mange arkivet matchede i alt
    archive_url: str = ""       # samme filter, aabnet i arkivets eget UI
    valuation: Valuation | None = None   # naar spoergsmaalet er hvad noget er vaerd
    meta: dict = field(default_factory=dict)   # det der gemmes i samtalen


def _extract_json(raw: str) -> dict:
    """Træk det første JSON-objekt ud af et modelsvar."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("intet JSON i svaret")
    return json.loads(match.group(0))


def _fallback_filter(question: str) -> SearchQuery:
    """Et rimeligt filter uden model: brug spørgsmålet som søgeord.

    Stopordene fjernes, så 'har der været nogen Sennheiser?' bliver til
    'sennheiser' i stedet for at søge på hele sætningen.
    """
    # Uden denne liste bliver "Hvilke NAS-enheder er der lige nu?" til
    # "nas enheder lige nu", hvor de sidste to ord ikke findes i nogen titel.
    stopwords = {
        "har", "der", "været", "nogen", "noget", "nogle", "hvad", "hvilke",
        "hvilken", "hvornår", "hvordan", "er", "en", "et", "og", "i", "på",
        "til", "med", "for", "af", "om", "jeg", "du", "den", "det", "de",
        "kan", "vil", "skal", "som", "under", "over", "find", "findes",
        "fandtes", "haft", "vis", "mig", "søg", "efter", "hvor", "mange",
        "lige", "nu", "gerne", "ca", "cirka", "omkring", "siden", "dage",
        "dag", "uge", "uger", "måned", "måneder", "år",
    }
    words = [
        w for w in re.findall(r"[\wæøåÆØÅ]+", question.lower())
        if w not in stopwords and len(w) > 2 and not w.isdigit()
    ]
    # OR, ikke AND: assistenten har ikke et struktureret filter at læne sig på
    # her, så et enkelt dækord må ikke kræve at alle de øvrige ord også står i
    # titlen.
    return SearchQuery(text=" ".join(words[:6]), any_words=True)


def _history_lines(history: list[dict] | None, limit: int = 4) -> str:
    """De sidste beskeder, korte. Vinduet holdes lille, saa prisen er flad."""
    if not history:
        return ""
    return "\n".join(
        f"{'Bruger' if m.get('role') == 'user' else 'Assistent'}: "
        f"{str(m.get('content') or '')[:200]}"
        for m in history[-limit:]
    )


def build_filter(
    client: OpenAICompatibleClient | None,
    question: str,
    categories: list[str] | None = None,
    history: list[dict] | None = None,
    previous: dict | None = None,
) -> tuple[SearchQuery, bool]:
    """(filter, degraded). Falder tilbage til nøgleordssøgning uden model."""
    if client is None:
        return _fallback_filter(question), True
    # Kategorinøglerne gives med i brugerbeskeden, så systemprompten er stabil.
    hint = ("\n\nKategorinøgler: " + ", ".join(categories)) if categories else ""
    earlier = _history_lines(history)
    if earlier:
        hint += f"\n\nTidligere samtale:\n{earlier}"
    if previous:
        hint += ("\n\nForrige filter (behold felterne medmindre spørgsmålet "
                 "ændrer dem): " + json.dumps(previous, ensure_ascii=False))
    try:
        raw = client.complete(system=FILTER_SYSTEM, user=question + hint, max_tokens=300)
        data = _extract_json(raw)
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        log.warning("Kunne ikke bygge filter med modellen: %s", exc)
        return _fallback_filter(question), True

    def as_int(key: str) -> int | None:
        value = data.get(key)
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def clean(value: object) -> str:
        return str(value).replace('"', " ").replace("'", " ").strip()

    # soegeord er listen; text holdes som bagudkompatibelt alias.
    words = data.get("soegeord")
    if isinstance(words, list):
        text = " ".join(part for part in (clean(word) for word in words) if part)
    else:
        text = clean(data.get("text") or "")

    query = SearchQuery(
        text=text[:200],
        min_price=as_int("min_price"),
        max_price=as_int("max_price"),
        status=str(data.get("status") or "alle"),
        matched=str(data.get("matched") or "alle"),
        category=str(data.get("kategori") or data.get("category") or ""),
        days_back=as_int("days_back"),
        sort=str(data.get("sort") or "relevans"),
        # OR som standard: modellen udvider selv med beslægtede produkttyper.
        any_words=not bool(data.get("alle_ord")),
        liste=clean(data.get("liste") or "").lower(),
    ).normalized()

    # Et tomt filter ville hente hele arkivet uden grund.
    if query.is_empty:
        return _fallback_filter(question), False
    return query, False


def _rows_for_model(rows: list[sqlite3.Row]) -> str:
    """Kompakt, nummereret gengivelse af søgeresultatet til modellen.

    Numrene er dem modellen svarer med, så den kan pege på rækker i stedet for
    at skulle gengive titler.
    """
    if not rows:
        return "(ingen lots fundet)"
    lines = []
    for number, row in enumerate(rows, start=1):
        price = kr(row["last_total"] or row["last_bid"])
        left, _, _ = time_left(row["ends_at"])
        status = left or "ukendt sluttidspunkt"
        matched = "matchede profilen" if row["was_match"] else "ikke et fund"
        # Din egen markering er en oplysning modellen kan svare ud fra, fx
        # "har jeg afvist noget lignende".
        action = row["feedback_action"] if "feedback_action" in row.keys() else ""
        feedback = f" | {_FEEDBACK_LABELS[action]}" if action in _FEEDBACK_LABELS else ""
        lines.append(f"{number}. {row['title']} | {price} | {status} | {matched}{feedback}")
    return "\n".join(lines)


def _price_summary(rows: list[sqlite3.Row]) -> str:
    """Median og spænd over de fundne lots, så modellen kan svare på hvad de koster."""
    prices = sorted(
        int(row["last_total"] or row["last_bid"])
        for row in rows
        if (row["last_total"] or row["last_bid"])
    )
    if not prices:
        return ""
    middle = len(prices) // 2
    median = (
        prices[middle] if len(prices) % 2
        else round((prices[middle - 1] + prices[middle]) / 2)
    )
    return (
        f"Priser i det fundne sæt: median {kr(median)}, laveste {kr(prices[0])}, "
        f"højeste {kr(prices[-1])}, over {len(prices)} af {len(rows)} lots med en pris."
    )


def _valuation(conn: sqlite3.Connection, row: sqlite3.Row) -> Valuation:
    """Det valgte lots egne tal, de sammenlignelige salg og prisforløbet."""
    try:
        comps = similar.find(conn, lot_id=row["lot_id"], title=row["title"] or "")
    except sqlite3.Error:
        comps = similar.Comparables()
    series = queries.price_series(conn, [row["lot_id"]]).get(row["lot_id"], [])
    return Valuation(
        lot_id=row["lot_id"],
        title=row["title"] or "",
        url=row["url"] or "",
        total=row["last_total"] or row["last_bid"],
        comps=comps,
        series=series,
    )


def _comparables_note(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], *, limit: int = 2, sales: int = 3
) -> str:
    """Hvad samme slags er gået for, for de øverste kandidater.

    Tallene kommer fra arkivets egne afsluttede salg, ikke fra modellen, så et
    svar om hvad noget er værd kan bygge på noget virkeligt.
    """
    parts: list[str] = []
    for row in rows[:limit]:
        try:
            comps = similar.find(conn, lot_id=row["lot_id"], title=row["title"] or "")
        except sqlite3.Error:
            continue
        if not comps.count or comps.median is None:
            continue
        head = (
            f"{(row['title'] or '')[:60]}: median {kr(comps.median)}, "
            f"laveste {kr(comps.low)}, højeste {kr(comps.high)} "
            f"over {comps.count} tidligere salg"
        )
        # De enkelte salg med pris og dato, saa svaret kan naevne konkrete tal
        # i stedet for kun et gennemsnit.
        examples = "; ".join(
            f"{kr(sale.total)} ({sale.ended_at[:10]})" for sale in comps.items[:sales]
        )
        parts.append(f"{head}. Eksempler: {examples}" if examples else head)
    return " ".join(parts)


def _selected_rows(rows: list[sqlite3.Row], valgte: Any) -> list[sqlite3.Row]:
    """Omsæt modellens numre til rækker. Ukendte numre ignoreres.

    Modellen kan svare med hvad som helst, så numrene valideres frem for at
    stole på dem. Et ugyldigt nummer må ikke kunne vælte siden.
    """
    if not isinstance(valgte, list):
        return []
    picked: list[sqlite3.Row] = []
    seen: set[int] = set()
    for value in valgte:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number in seen or not 1 <= number <= len(rows):
            continue
        seen.add(number)
        picked.append(rows[number - 1])
    return picked


def archive_url(query: SearchQuery) -> str:
    """Samme filter som assistenten brugte, åbnet i arkivets eget UI.

    Arkivet er stedet hvor filtrene kan rettes, så et svar der bygger på en
    forkert fortolkning er ét klik fra at blive rigtigt.
    """
    params: dict[str, object] = {}
    if query.text:
        params["q"] = query.text
    if query.min_price:
        params["min_price"] = query.min_price
    if query.max_price:
        params["max_price"] = query.max_price
    if query.status != "alle":
        params["status"] = query.status
    if query.matched != "alle":
        params["matched"] = query.matched
    if query.category:
        params["category"] = query.category
    if query.days_back:
        params["days_back"] = query.days_back
    if query.sort != "relevans":
        params["sort"] = query.sort
    qs = urlencode(params)
    return f"/archive?{qs}" if qs else "/archive"


def _filter_dict(query: SearchQuery) -> dict:
    """Det strukturerede filter, saa naeste tur kan bygge videre paa det."""
    return {
        key: value for key, value in {
            "soegeord": query.text,
            "min_price": query.min_price,
            "max_price": query.max_price,
            "status": query.status if query.status != "alle" else None,
            "matched": query.matched if query.matched != "alle" else None,
            "kategori": query.category or None,
            "days_back": query.days_back,
            "sort": query.sort if query.sort != "relevans" else None,
            "alle_ord": True if not query.any_words else None,
            "liste": query.liste or None,
        }.items() if value is not None
    }


def _valuation_dict(valuation: Valuation) -> dict:
    comps = valuation.comps
    return {
        "lot_id": valuation.lot_id,
        "title": valuation.title,
        "url": valuation.url,
        "total": valuation.total,
        "series": list(valuation.series),
        "comps": None if comps is None else {
            "count": comps.count,
            "median": comps.median,
            "low": comps.low,
            "high": comps.high,
            "items": [
                {"lot_id": sale.lot_id, "title": sale.title, "hammer": sale.hammer,
                 "total": sale.total, "ended_at": sale.ended_at,
                 "shared": list(sale.shared)}
                for sale in comps.items
            ],
        },
    }


def ask(
    conn: sqlite3.Connection,
    client: OpenAICompatibleClient | None,
    question: str,
    *,
    show_all: bool = False,
    categories: list[str] | None = None,
    history: list[dict] | None = None,
    previous: dict | None = None,
    labels: dict[str, str] | None = None,
) -> ChatAnswer:
    """Besvar et spørgsmål om arkivet.

    Modellen vælger selv hvilke af kandidaterne der er svar på spørgsmålet.
    Fejler den, eller beder brugeren om alt, vises hele søgeresultatet i
    stedet — det må ikke koste et fund at modellen svigter.
    """
    question = question.strip()[:MAX_QUESTION_LENGTH]
    if not question:
        return ChatAnswer(text="Stil et spørgsmål, så leder jeg i arkivet.")

    query, degraded = build_filter(
        client, question, categories, history=history, previous=previous
    )

    note = ""

    def finish(answer: ChatAnswer) -> ChatAnswer:
        # url og filter laeses fra query ved kald, saa en redning nedenfor
        # afspejles baade i linket og i det viste filter.
        answer.archive_url = archive_url(query)
        answer.meta = {
            "filter": _filter_dict(query),
            "filter_used": answer.filter_used,
            "selected": [row["lot_id"] for row in answer.rows],
        }
        if answer.valuation is not None:
            answer.meta["valuation"] = _valuation_dict(answer.valuation)
        if note:
            answer.text = f"{note}\n\n{answer.text}"
        return answer

    # Rangordning i stedet for en soegning: "hvad er gaaet mest op i pris".
    if query.liste == "stigere":
        rows = queries.risers(conn, limit=12)
        if not rows:
            return finish(ChatAnswer(
                text="Ingen lots i arkivet er steget i pris siden vi så dem først."
            ))
        top = rows[0]
        rise = int(top["last_bid"] or 0) - int(top["first_bid"] or 0)
        return finish(ChatAnswer(
            text=(f"De største prisstigninger lige nu. Øverst ligger "
                  f"{top['title']} med {kr(rise)} over det første bud vi så."),
            rows=rows, selected=True, considered=len(rows), total=len(rows),
        ))

    result = search(conn, query)

    # Bliver der intet, løsnes filtrene et ad gangen, fra det mest specifikke
    # til det bredeste. Søgeordene fjernes aldrig: det var netop den fejl der
    # fik et spørgsmål om et server rack til at blive besvaret med en switch.
    if result.total == 0 and query.text and not query.any_words:
        relaxed = replace(query, any_words=True)
        alternative = search(conn, relaxed)
        if alternative.total:
            result, query = alternative, relaxed
            note = ("Jeg fandt intet med alle ordene, så jeg søgte efter dem "
                    "hver for sig.")
    if result.total == 0 and query.category:
        # En kategori kræver en notifikation, så den skjuler alle ikke-fund.
        relaxed = replace(query, category="")
        alternative = search(conn, relaxed)
        if alternative.total:
            result, query = alternative, relaxed
            note = "Jeg fandt intet i den kategori, så jeg søgte i hele arkivet."
    if result.total == 0 and query.matched != "alle":
        relaxed = replace(query, matched="alle")
        alternative = search(conn, relaxed)
        if alternative.total:
            result, query = alternative, relaxed
            note = "Jeg fandt intet med det filter, så jeg søgte i hele arkivet."

    candidates = result.rows[:MAX_CANDIDATES]

    # Værdierne formateres, så de kan læses som dansk og ikke som feltnavne.
    filter_used = {
        key: value for key, value in {
            "søgeord": f'"{query.text}"' if query.text else None,
            "min_pris": kr(query.min_price) if query.min_price else None,
            "maks_pris": kr(query.max_price) if query.max_price else None,
            "status": {"aktive": "kun aktive",
                       "afsluttede": "kun afsluttede"}.get(query.status),
            "kun": {"kun_fund": "kun fund",
                    "kun_ikke_fund": "kun ikke-fund"}.get(query.matched),
            "kategori": ((labels or {}).get(query.category, query.category)
                         if query.category else None),
            "dage_tilbage": f"{query.days_back} dage" if query.days_back else None,
        }.items() if value
    }

    if client is None:
        return finish(ChatAnswer(
            text=(
                f"Fandt {result.total} lots. "
                "AI-svar kræver at CLASSIFIER_API_KEY er sat — indtil da viser "
                "jeg resultaterne direkte."
            ),
            rows=candidates,
            filter_used=filter_used,
            degraded=True,
            total=result.total,
        ))

    if show_all or not candidates:
        return finish(ChatAnswer(
            text=f"Fandt {result.total} lots." if candidates else
                 "Ingen lots i arkivet matcher spørgsmålet.",
            rows=candidates,
            filter_used=filter_used,
            considered=len(candidates),
            total=result.total,
        ))

    context_lines = [line for line in (
        _price_summary(candidates),
        (f"Tidligere salg af samme slags: {_comparables_note(conn, candidates)}"
         if candidates else ""),
    ) if line]
    context = ("\n" + "\n".join(context_lines) + "\n") if context_lines else ""

    earlier = _history_lines(history)
    user = (
        (f"Tidligere samtale:\n{earlier}\n\n" if earlier else "")
        + f"Spørgsmål: {question}\n\n"
        + f"Databasen fandt {result.total} lots. Her er {len(candidates)},\n"
        + "nummereret fra 1:\n"
        + f"{_rows_for_model(candidates)}"
        + f"{context}"
    )
    try:
        raw = client.complete(system=ANSWER_SYSTEM, user=user, max_tokens=700)
    except LLMError as exc:
        log.warning("Kunne ikke formulere svar: %s", exc)
        return finish(ChatAnswer(
            text=f"Fandt {result.total} lots, men kunne ikke nå sprogmodellen.",
            rows=candidates,
            filter_used=filter_used,
            degraded=True,
            total=result.total,
        ))

    # Modellen skal svare med JSON, men gør det ikke altid. Kan svaret ikke
    # læses, vises hele søgeresultatet og teksten bruges som den er.
    try:
        payload = _extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        return finish(ChatAnswer(
            text=raw.strip(),
            rows=candidates,
            filter_used=filter_used,
            considered=len(candidates),
            total=result.total,
        ))

    picked = _selected_rows(candidates, payload.get("valgte"))
    text = str(payload.get("svar") or "").strip() or raw.strip()

    # Modellen kan pege paa den kandidat spoergsmaalet handler om. Nummeret
    # valideres mod listen, og resten bygges af Python.
    valuation = None
    if payload.get("sammenlign") is not None:
        subject = _selected_rows(candidates, [payload.get("sammenlign")])
        if subject:
            valuation = _valuation(conn, subject[0])

    return finish(ChatAnswer(
        text=text,
        rows=picked,
        filter_used=filter_used,
        degraded=degraded,
        considered=len(candidates),
        selected=True,
        total=result.total,
        valuation=valuation,
    ))


# -- forslag til manglende noegleord ---------------------------------------

def suggest_keywords(
    config: Config,
    client: OpenAICompatibleClient | None,
    titles: list[str],
    *,
    limit: int = 6,
) -> list[dict]:
    """Lad modellen foreslå nøgleord for titler profilen ikke fangede.

    Returnerer modellens rå forslag; valideringen mod den faktiske
    konfiguration sker i suggest.from_model. Enhver fejl giver en tom liste,
    for et teknisk problem må ikke gøre interessesiden ubrugelig.
    """
    if client is None or not titles:
        return []

    listing = "\n".join(f"{i}. {title}" for i, title in enumerate(titles[:12], start=1))
    categories = ", ".join(cat.key for cat in config.categories)
    user = (
        "## Titler brugeren har budt på eller købt, som ikke blev fundet\n"
        f"{listing}\n\n"
        f"## Kategorier (nøgle)\n{categories}\n\n"
        "## Opgave\n"
        "Foreslå højst ét nøgleord pr. titel, og spring titlen over hvis intet\n"
        "giver mening. Nøgleordet skal stå i titlen. strong er en konkret\n"
        "produkttype, weak et bredt ord, brands et mærkenavn (aldrig i strong),\n"
        "exact et mærke der også er en del af andre ord.\n\n"
        'Svar med JSON: {"forslag": [{"titel": "...", "noegleord": "...", '
        '"kategori": "<noegle>", "niveau": "strong|weak|brands|exact", '
        '"grund": "kort begrundelse"}]}'
    )
    try:
        raw = client.complete(system=SUGGEST_SYSTEM, user=user, max_tokens=800)
        payload = _extract_json(raw)
    except Exception as exc:  # fail-open, som resten af AI-trinnet
        log.warning("Kunne ikke hente noegleordsforslag: %s", exc)
        return []

    rows = payload.get("forslag")
    return ([row for row in rows if isinstance(row, dict)][:limit]
            if isinstance(rows, list) else [])


# -- forklaring af profilen ------------------------------------------------

@dataclass
class MatchTrace:
    """Hvad matcheren så på en titel."""

    title: str
    matched: bool
    category: str = ""
    keywords: tuple[str, ...] = ()
    excluded_by: tuple[str, ...] = ()
    near_misses: list[dict] = field(default_factory=list)

    def as_text(self, labels: dict[str, str] | None = None) -> str:
        """Forklaringen på dansk. labels oversætter kategorinøgler til etiketter."""
        def pretty(key: str) -> str:
            key = key.strip()
            return (labels or {}).get(key, key)

        if self.excluded_by:
            return (
                f"Titlen blev udelukket af ordet/ordene: "
                f"{', '.join(self.excluded_by)}. Udelukkelser vinder over alt andet."
            )
        if self.matched:
            names = ", ".join(pretty(n) for n in self.category.split(","))
            return f"Matchede kategorien '{names}' på: {', '.join(self.keywords)}."
        if self.near_misses:
            parts = [
                f"'{pretty(m['category'])}' ramte {m['hits']} men manglede "
                f"{m['missing']}"
                for m in self.near_misses
            ]
            return "Matchede ikke. Tæt på: " + "; ".join(parts) + "."
        return "Matchede ikke. Ingen af kategoriernes nøgleord blev ramt."


def explain_title(config: Config, title: str) -> MatchTrace:
    """Kør den rigtige matcher på en titel og fortæl hvad der skete.

    Bruger samme kode som agenten, så forklaringen ikke kan komme til at
    afvige fra virkeligheden.
    """
    title = title.strip()[:300]
    excluded = config.is_excluded(title)
    if excluded:
        return MatchTrace(title=title, matched=False, excluded_by=excluded)

    lot = Lot(
        lot_id="preview", title=title, url="", lot_number="",
        auction_id="", auction_title="", current_bid=None, total_price=None,
        ends_at=None, image_url="", has_bids=False,
    )
    matches = match_lot(lot, config, opening_bid=config.opening_bid)
    if matches:
        first = matches[0]
        return MatchTrace(
            title=title, matched=True,
            category=", ".join(m.category.key for m in matches),
            keywords=first.keywords,
        )

    # Ikke et match: find ud af hvor tæt hver kategori var.
    near: list[dict] = []
    for category in config.categories:
        weak = find_keywords(title, category.weak, exact=category.exact)
        brands = find_keywords(title, category.brands, exact=category.exact)
        if not (weak or brands):
            continue
        if brands and not weak:
            missing = "en produkttype ved siden af mærket"
        elif len(set(weak)) == 1:
            missing = "endnu et forskelligt weak-ord (der kræves to)"
        else:
            missing = "et strong-ord"
        near.append({
            "category": category.key,
            "hits": ", ".join(weak + brands),
            "missing": missing,
        })

    return MatchTrace(title=title, matched=False, near_misses=near)


def explain(
    config: Config,
    client: OpenAICompatibleClient | None,
    title: str,
) -> tuple[MatchTrace, str]:
    """(analyse, prosaforklaring). Analysen står altid, også uden model."""
    trace = explain_title(config, title)
    if client is None:
        return trace, ""

    user = (
        f"Lot-titel: {title}\n"
        f"Analyse fra matcheren: {trace.as_text()}\n\n"
        "Forklar kort, og foreslå en konkret ændring hvis den er relevant."
    )
    try:
        return trace, client.complete(
            system=EXPLAIN_SYSTEM, user=user, max_tokens=400
        ).strip()
    except LLMError as exc:
        log.warning("Kunne ikke forklare match: %s", exc)
        return trace, ""
