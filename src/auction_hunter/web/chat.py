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
MAX_RESULTS_TO_MODEL = 12

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

ANSWER_SYSTEM = (
    "Du er en hjælpsom assistent for en dansk auktionsagent. "
    "Du får et spørgsmål og de lots databasen fandt. "
    "Svar kort og konkret på dansk. Nævn priser og hvornår ting slutter når det "
    "er relevant. Find ikke på lots der ikke står i listen. "
    "Er listen tom, så sig det ligeud."
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
    degraded: bool = False   # True når modellen ikke kunne nås


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
    stopwords = {
        "har", "der", "været", "nogen", "noget", "hvad", "hvilke", "hvilken",
        "er", "en", "et", "og", "i", "på", "til", "med", "for", "af", "om",
        "jeg", "du", "den", "det", "de", "kan", "vil", "skal", "som", "under",
        "over", "find", "vis", "mig", "søg", "efter", "hvor", "mange",
    }
    words = [
        w for w in re.findall(r"[\wæøåÆØÅ]+", question.lower())
        if w not in stopwords and len(w) > 2 and not w.isdigit()
    ]
    return SearchQuery(text=" ".join(words[:6]))


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
    """Kompakt gengivelse af søgeresultatet til modellen."""
    if not rows:
        return "(ingen lots fundet)"
    lines = []
    for row in rows[:MAX_RESULTS_TO_MODEL]:
        price = kr(row["last_total"] or row["last_bid"])
        left, _, _ = time_left(row["ends_at"])
        status = left or "ukendt sluttidspunkt"
        matched = "matchede profilen" if row["was_match"] else "ikke et fund"
        lines.append(f"- {row['title']} | {price} | {status} | {matched}")
    return "\n".join(lines)


def ask(
    conn: sqlite3.Connection,
    client: OpenAICompatibleClient | None,
    question: str,
) -> ChatAnswer:
    """Besvar et spørgsmål om arkivet."""
    question = question.strip()[:MAX_QUESTION_LENGTH]
    if not question:
        return ChatAnswer(text="Stil et spørgsmål, så leder jeg i arkivet.")

    query, degraded = build_filter(client, question)
    result = search(conn, query)

    filter_used = {
        k: v for k, v in {
            "søgeord": query.text,
            "min_pris": query.min_price,
            "maks_pris": query.max_price,
            "status": query.status if query.status != "alle" else None,
            "kun": query.matched if query.matched != "alle" else None,
            "dage_tilbage": query.days_back,
        }.items() if v
    }

    if client is None:
        return ChatAnswer(
            text=(
                f"Fandt {result.total} lots. "
                "AI-svar kræver at CLASSIFIER_API_KEY er sat — indtil da viser "
                "jeg resultaterne direkte."
            ),
            rows=result.rows,
            filter_used=filter_used,
            degraded=True,
        )

    user = (
        f"Spørgsmål: {question}\n\n"
        f"Databasen fandt {result.total} lots. De første:\n"
        f"{_rows_for_model(result.rows)}"
    )
    try:
        text = client.complete(system=ANSWER_SYSTEM, user=user, max_tokens=500)
    except LLMError as exc:
        log.warning("Kunne ikke formulere svar: %s", exc)
        return ChatAnswer(
            text=f"Fandt {result.total} lots, men kunne ikke nå sprogmodellen.",
            rows=result.rows,
            filter_used=filter_used,
            degraded=True,
        )

    return ChatAnswer(
        text=text.strip(),
        rows=result.rows,
        filter_used=filter_used,
        degraded=degraded,
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

    def as_text(self) -> str:
        if self.excluded_by:
            return (
                f"Titlen blev udelukket af ordet/ordene: "
                f"{', '.join(self.excluded_by)}. Udelukkelser vinder over alt andet."
            )
        if self.matched:
            return (
                f"Matchede kategorien '{self.category}' på: "
                f"{', '.join(self.keywords)}."
            )
        if self.near_misses:
            parts = [
                f"'{m['category']}' ramte {m['hits']} men manglede "
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
