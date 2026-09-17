"""AI-klassificering af fund som andet led efter nøgleordsmatcheren.

Nøgleord er gode til at *finde* kandidater og billige at køre på alle lots,
men de kan ikke læse kontekst. De fanger derfor lot-titler som
'Div. lydudstyr: DVI-forlængere, HDD-adaptere mv.' fordi ordet 'hdd' står der,
selvom varen er et rodlot med adaptere.

Dette modul lægger et sprogmodeltrin ovenpå: kun de lots der allerede har
matchet på nøgleord bliver vurderet, så omkostningen følger antallet af fund
og ikke antallet af lots. Modellen svarer i tre kategorier:

``ja``     fundet er reelt interessant og sendes med det samme
``nej``    fundet er støj og undertrykkes
``måske``  grænsetilfælde. Gemmes i en kø og sendes samlet i et digest, så
           brugeren selv kan afgøre det, uden at det støjer i nuet

Svarene caches i SQLite på et hash af titel, kategori, version og model.
Hashet betyder at et ændret titel giver et nyt opslag, og versionsnummeret
betyder at en ændret prompt eller model ugyldiggør cachen i ét hug. Det gør
AI-trinnet versionerbart på samme måde som facitlisten: beslutningerne kan
ændres uden at skulle genkøre eller røre de indsamlede data.

Kilden er en OpenAI-kompatibel ``/chat/completions``-endpoint, så både OpenAI,
Azure, OpenRouter, Ollama og vLLM kan bruges. Der er ingen ny afhængighed —
kaldet går gennem ``requests``, som allerede er i brug.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import requests

log = logging.getLogger(__name__)

# Bumpes når prompten eller svarformatet ændres, så cachen ugyldiggøres.
CLASSIFIER_VERSION = "1"

DEFAULT_TIMEOUT = 30
_MAX_REASON_LENGTH = 200


class LLMError(RuntimeError):
    """Kaldet til sprogmodellen fejlede."""


class Verdict(str, Enum):
    YES = "ja"
    NO = "nej"
    MAYBE = "maaske"

    @property
    def label(self) -> str:
        return {"ja": "ja", "nej": "nej", "maaske": "måske"}[self.value]

# Modeller skriver sjældent præcis det man beder om. Disse synonymer foldes til
# de tre kanoniske værdier, så et svar som "Ja, helt sikkert" ikke fejler.
_VERDICT_SYNONYMS = {
    "ja": Verdict.YES, "yes": Verdict.YES, "true": Verdict.YES, "y": Verdict.YES,
    "nej": Verdict.NO, "no": Verdict.NO, "false": Verdict.NO, "n": Verdict.NO,
    "maaske": Verdict.MAYBE, "måske": Verdict.MAYBE, "maybe": Verdict.MAYBE,
    "muligvis": Verdict.MAYBE, "usikker": Verdict.MAYBE, "?": Verdict.MAYBE,
}

SYSTEM_PROMPT = (
    "Du er et præcist filter for en dansk auktionsagent. "
    "Du får en interesseprofil og et lot fra en auktion og skal afgøre om lotet "
    "er noget brugeren vil have. Svar KUN med et JSON-objekt og intet andet "
    "tekst omkring det."
)


def build_user_prompt(*, title: str, category_label: str, keywords: tuple[str, ...], profile: str) -> str:
    """Byg brugerprompten ud fra profilen og det konkrete lot."""
    matched = ", ".join(keywords) if keywords else "(ingen)"
    return (
        f"## Interesseprofil\n{profile.strip()}\n\n"
        f"## Lot\n"
        f"Titel: {title}\n"
        f"Foreslået kategori: {category_label}\n"
        f"Nøgleord der udløste forslaget: {matched}\n\n"
        "## Opgave\n"
        "Vurder om lotet er interessant for brugeren. Vær opmærksom på at en "
        "titel kan indeholde et nøgleord uden at varen er interessant — fx et "
        "rodlot, tilbehør eller en helt anden vare der tilfældigt deler ordet.\n\n"
        'Svar med JSON: {"verdict": "ja" | "nej" | "maaske", "reason": "kort '
        'begrundelse på dansk, maks. 20 ord"}'
    )


def parse_verdict(raw: str) -> tuple[Verdict, str] | None:
    """Træk et verdict ud af modellens svar. None hvis det ikke kan læses."""
    if not raw or not raw.strip():
        return None

    text = raw.strip()
    # Fjern markdown-hegn hvis modellen har pakket svaret ind alligevel.
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    payload = None
    try:
        payload = json.loads(text)
    except ValueError:
        # Modellen kan have skrevet tekst omkring JSON-objektet.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
            except ValueError:
                payload = None

    if isinstance(payload, dict):
        raw_verdict = str(payload.get("verdict", "")).strip().lower()
        reason = str(payload.get("reason", "")).strip()
    else:
        # Intet JSON — prøv at læse det første ord som svar.
        raw_verdict = text.split()[0].strip(".,:;\"'").lower() if text else ""
        reason = ""

    verdict = _VERDICT_SYNONYMS.get(raw_verdict)
    if verdict is None:
        return None
    return verdict, reason[:_MAX_REASON_LENGTH]


def input_hash(
    *, title: str, category_key: str, version: str, model: str, profile: str = ""
) -> str:
    """Stabil nøgle for et klassificeringssvar.

    Titlen normaliseres let, så forskelle i mellemrum og store/små bogstaver
    ikke giver to opslag for samme vare. Ændres titlen derimod reelt — fx fordi
    auktionshuset retter den — giver det et nyt opslag, hvilket er hensigten.

    ``profile`` er med, fordi den *er* prompten. Den står i interests.yml,
    README kalder den "den egentlige smag", og brugeren opfordres til at
    redigere den. Uden den i nøglen genbrugte cachen gamle domme i op til 180
    dage efter en rettelse, og den eneste vej til at ugyldiggøre dem var at
    hæve en konstant i kildekoden. Teksten hashes for sig, så nøglen har
    samme længde uanset hvor lang profilen er.
    """
    collapsed = " ".join(title.lower().split())
    profile_digest = hashlib.sha256(
        " ".join(profile.split()).encode("utf-8")
    ).hexdigest()[:16]
    material = "\x1f".join(
        (collapsed, category_key, version, model, profile_digest)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)


class Classification:
    verdict: Verdict
    reason: str
    model: str
    source: str  # 'cache', 'model' eller 'error'


@dataclass


class ClassifierSettings:
    """Indstillinger for AI-trinnet.

    ``profile`` er brugerens egen beskrivelse af smag og afvejninger. Den står i
    interests.yml sammen med nøgleordene, så begge dele versionsfølges i git.
    """

    enabled: bool = False
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    profile: str = ""
    max_per_run: int = 25
    timeout: int = DEFAULT_TIMEOUT
    version: str = CLASSIFIER_VERSION

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.api_key and self.profile.strip())


def _requests_transport(*, url: str, headers: dict, payload: dict, timeout: int) -> tuple[int, dict]:
    """Standardtransport. Injiceres i test, så intet netværk rammes der."""
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"raw": response.text[:500]}


class OpenAICompatibleClient:
    """Minimal klient mod en OpenAI-kompatibel chat-endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        transport: Callable[..., tuple[int, dict]] = _requests_transport,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.transport = transport

    def complete(self, *, system: str, user: str, max_tokens: int = 120) -> str:
        """Ét kald til modellen.

        ``max_tokens`` er lavt som standard, fordi klassificeringen kun skal
        svare med et lille JSON-objekt. Chatten i webdashboardet sætter den op.
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # temperature 0 gør svaret så stabilt som muligt, så cachen holder.
            "temperature": 0,
            "max_tokens": max_tokens,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                status, body = self.transport(
                    url=url, headers=headers, payload=payload, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Netværksfejl mod sprogmodellen (forsøg %d/%d): %s",
                            attempt, self.max_retries, exc)
            else:
                if status == 200:
                    try:
                        return body["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise LLMError(f"Uventet svarformat fra modellen: {body}") from exc
                if (status == 400 and "max_tokens" in payload
                        and "max_tokens" in str(body).lower()):
                    # Nyere modeller (o-serien) afviser max_tokens og vil have
                    # max_completion_tokens. Proev én gang med det rigtige navn.
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    log.info("Modellen afviste max_tokens — proever max_completion_tokens")
                    continue
                if status in (429, 500, 502, 503, 504):
                    last_error = LLMError(f"HTTP {status}: {str(body)[:200]}")
                    log.warning("Sprogmodellen svarede %s (forsøg %d/%d)",
                                status, attempt, self.max_retries)
                else:
                    raise LLMError(f"Modellen afviste kaldet (HTTP {status}): {str(body)[:300]}")
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 10))
        raise LLMError(f"Kunne ikke få svar efter {self.max_retries} forsøg: {last_error}")


@dataclass


class ClassifyResult:
    """Udfaldet af at klassificere en omgang fund."""

    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    review: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    errors: int = 0


class Classifier:
    """Klassificerer matchede lots og husker svarene."""

    def __init__(self, client: OpenAICompatibleClient, settings: ClassifierSettings, store) -> None:
        self.client = client
        self.settings = settings
        self.store = store

    def profile_for(self, match) -> str:
        """Den smagsbeskrivelse dette fund skal vurderes efter.

        En interesseprofil kan have sin egen ``classifier_profile``. Feltet
        fandtes allerede i dataklassen og blev laest fra YAML, men blev aldrig
        brugt: alle profilers fund blev bedoemt med den globale prompt. Alle
        profilens oevrige felter respekteres — kategorier, prisloft,
        udelukkelser, webhook — saa det her var det eneste der ikke virkede,
        og det gjorde det tavst.
        """
        own = getattr(match.profile, "classifier_profile", "") or ""
        return own.strip() or self.settings.profile

    def _key(self, match) -> str:
        """Cache-nøglen for et fund. Ét sted, så profilen ikke kan glemmes.

        Den effektive smagsbeskrivelse indgaar, saa to interesseprofiler med
        hver sin ``classifier_profile`` faar hver sit svar paa samme lot i
        stedet for at dele det foerstes.
        """
        return input_hash(
            title=match.lot.title,
            category_key=match.category.key,
            version=self.settings.version,
            model=self.settings.model,
            profile=self.profile_for(match),
        )

    def cached(self, match) -> Classification | None:
        """Et tidligere svar, hvis der er et. Koster intet kald.

        Skilt ud fra ``classify_one``, så loftet pr. kørsel kan skelne mellem
        et cache-opslag og et rigtigt kald til modellen.
        """
        row = self.store.cached_classification(self._key(match))
        if row is None:
            return None
        try:
            verdict = Verdict(row["verdict"])
        except ValueError:
            log.warning("Ukendt verdict i cachen for %s: %r",
                        match.lot.lot_id, row["verdict"])
            return None
        return Classification(verdict, row["reason"], row["model"], "cache")

    def classify_one(self, match) -> Classification:
        """Klassificér ét fund, med cache-opslag først."""
        key = self._key(match)

        cached = self.store.cached_classification(key)
        if cached is not None:
            try:
                verdict = Verdict(cached["verdict"])
            except ValueError:
                # En ukendt vaerdi i cachen (fx efter en manuel rettelse i
                # databasen) maa ikke vaelte hele AI-trinnet. Behandl den som
                # et cache-miss og spoerg modellen igen.
                log.warning("Ukendt verdict i cachen for %s: %r",
                            match.lot.lot_id, cached["verdict"])
            else:
                return Classification(
                    verdict=verdict,
                    reason=cached["reason"],
                    model=cached["model"],
                    source="cache",
                )

        prompt = build_user_prompt(
            title=match.lot.title,
            category_label=match.category.label,
            keywords=match.keywords,
            profile=self.profile_for(match),
        )
        try:
            raw = self.client.complete(system=SYSTEM_PROMPT, user=prompt)
            parsed = parse_verdict(raw)
        except LLMError as exc:
            # Fail-open: et svar vi ikke kan få, må ikke koste et rigtigt fund.
            log.error("Klassificering fejlede for %r: %s", match.lot.title[:50], exc)
            return Classification(Verdict.YES, "", self.settings.model, "error")
        except Exception as exc:
            # En klient der ikke fejler som requests (proxy, manglende pakke,
            # vilkårligt bibliotek) må heller ikke koste fundet.
            log.exception("Uventet fejl under klassificering af %r: %s",
                          match.lot.title[:50], exc)
            return Classification(Verdict.YES, "", self.settings.model, "error")

        if parsed is None:
            log.warning("Kunne ikke læse modelsvaret for %r — beholder fundet",
                        match.lot.title[:50])
            return Classification(Verdict.YES, "", self.settings.model, "error")

        verdict, reason = parsed
        self.store.save_classification(
            key,
            lot_id=match.lot.lot_id,
            category_key=match.category.key,
            version=self.settings.version,
            model=self.settings.model,
            verdict=verdict.value,
            reason=reason,
        )
        return Classification(verdict, reason, self.settings.model, "model")

    def classify_matches(self, matches: list) -> ClassifyResult:
        """Del fund i accepterede, afviste, til gennemsyn og udskudte.

        Rækkefølgen er matcherens (billigst først), så et loft på antallet
        rammer de dyreste fund først. Fund ud over loftet udskydes i stedet for
        at blive sendt uklassificeret — de prøves igen ved næste kørsel.

        Loftet tæller kald til modellen, ikke fund. Et cache-opslag koster
        ingenting, og før talte det alligevel med: 25 cachede fund kunne bruge
        hele budgettet uden at der blev ringet én gang, så det 26. blev udskudt
        uden grund.
        """
        result = ClassifyResult()
        budget = self.settings.max_per_run
        calls = 0

        for match in matches:
            classification = self.cached(match)
            if classification is None:
                if calls >= budget:
                    result.deferred.append(match)
                    continue
                calls += 1
                classification = self.classify_one(match)

            # Grænsetilfælde sendes ikke nu, men lægges i kø til digest.
            if classification.verdict is Verdict.MAYBE:
                self.store.enqueue_review(
                    self._key(match),
                    lot_id=match.lot.lot_id,
                    category_key=match.category.key,
                    title=match.lot.title,
                    url=match.lot.url,
                    reason=classification.reason,
                )
                result.review.append(match)
            elif classification.verdict is Verdict.NO:
                result.rejected.append(match)
            else:
                result.accepted.append(match)

            if classification.source == "error":
                result.errors += 1

        if result.deferred:
            log.info("%d fund udskudt til næste kørsel (loft på %d pr. kørsel)",
                     len(result.deferred), budget)
        return result
