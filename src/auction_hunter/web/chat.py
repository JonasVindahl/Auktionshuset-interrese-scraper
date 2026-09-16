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
from dataclasses import dataclass, field
from typing import Any

from ..classifier import LLMError, OpenAICompatibleClient
from ..config import Config
from ..matcher import match_lot
from ..scraper import Lot
from ..textmatch import find_keywords
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
    '  text        søgeord, fx "sennheiser forstærker"\n'
    "  min_price   heltal, kroner\n"
    "  max_price   heltal, kroner\n"
    '  status      "alle" | "aktive" | "afsluttede"\n'
    '  matched     "alle" | "kun_fund" | "kun_ikke_fund"\n'
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
    "er relevant. Find ikke på lots der ikke står i listen."
)

EXPLAIN_SYSTEM = (
    "Du hjælper med at finjustere en dansk auktionsagents nøgleordsprofil. "
    "Du får en lot-titel og en analyse af hvorfor den matchede eller ikke "
    "matchede. Forklar kort hvorfor, og foreslå konkret hvilket nøgleord der "
    "skal tilføjes eller udelukkes, og på hvilket niveau "
    "(strong/weak/brands/exact). Svar på dansk, maks. 5 linjer."
)


@dataclass
class ChatAnswer:
    text: str
    rows: list[sqlite3.Row] = field(default_factory=list)
    filter_used: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False      # True når modellen ikke kunne nås
    considered: int = 0         # hvor mange kandidater modellen fik at vælge fra
    selected: bool = False      # True når modellen selv valgte rækkerne
    total: int = 0              # hvor mange arkivet matchede i alt


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


def build_filter(client: OpenAICompatibleClient | None, question: str) -> tuple[SearchQuery, bool]:
    """(filter, degraded). Falder tilbage til nøgleordssøgning uden model."""
    if client is None:
        return _fallback_filter(question), True
    try:
        raw = client.complete(system=FILTER_SYSTEM, user=question, max_tokens=200)
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

    query = SearchQuery(
        text=str(data.get("text") or "")[:200],
        min_price=as_int("min_price"),
        max_price=as_int("max_price"),
        status=str(data.get("status") or "alle"),
        matched=str(data.get("matched") or "alle"),
        days_back=as_int("days_back"),
        sort=str(data.get("sort") or "relevans"),
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
        lines.append(f"{number}. {row['title']} | {price} | {status} | {matched}")
    return "\n".join(lines)


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


def ask(
    conn: sqlite3.Connection,
    client: OpenAICompatibleClient | None,
    question: str,
    *,
    show_all: bool = False,
) -> ChatAnswer:
    """Besvar et spørgsmål om arkivet.

    Modellen vælger selv hvilke af kandidaterne der er svar på spørgsmålet.
    Fejler den, eller beder brugeren om alt, vises hele søgeresultatet i
    stedet — det må ikke koste et fund at modellen svigter.
    """
    question = question.strip()[:MAX_QUESTION_LENGTH]
    if not question:
        return ChatAnswer(text="Stil et spørgsmål, så leder jeg i arkivet.")

    query, degraded = build_filter(client, question)
    result = search(conn, query)
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
            "dage_tilbage": f"{query.days_back} dage" if query.days_back else None,
        }.items() if value
    }

    if client is None:
        return ChatAnswer(
            text=(
                f"Fandt {result.total} lots. "
                "AI-svar kræver at CLASSIFIER_API_KEY er sat — indtil da viser "
                "jeg resultaterne direkte."
            ),
            rows=candidates,
            filter_used=filter_used,
            degraded=True,
            total=result.total,
        )

    if show_all or not candidates:
        return ChatAnswer(
            text=f"Fandt {result.total} lots." if candidates else
                 "Ingen lots i arkivet matcher spørgsmålet.",
            rows=candidates,
            filter_used=filter_used,
            considered=len(candidates),
            total=result.total,
        )

    user = (
        f"Spørgsmål: {question}\n\n"
        f"Databasen fandt {result.total} lots. Her er {len(candidates)},\n"
        f"nummereret fra 1:\n"
        f"{_rows_for_model(candidates)}"
    )
    try:
        raw = client.complete(system=ANSWER_SYSTEM, user=user, max_tokens=700)
    except LLMError as exc:
        log.warning("Kunne ikke formulere svar: %s", exc)
        return ChatAnswer(
            text=f"Fandt {result.total} lots, men kunne ikke nå sprogmodellen.",
            rows=candidates,
            filter_used=filter_used,
            degraded=True,
            total=result.total,
        )

    # Modellen skal svare med JSON, men gør det ikke altid. Kan svaret ikke
    # læses, vises hele søgeresultatet og teksten bruges som den er.
    try:
        payload = _extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        return ChatAnswer(
            text=raw.strip(),
            rows=candidates,
            filter_used=filter_used,
            considered=len(candidates),
            total=result.total,
        )

    picked = _selected_rows(candidates, payload.get("valgte"))
    text = str(payload.get("svar") or "").strip() or raw.strip()
    return ChatAnswer(
        text=text,
        rows=picked,
        filter_used=filter_used,
        degraded=degraded,
        considered=len(candidates),
        selected=True,
        total=result.total,
    )


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
