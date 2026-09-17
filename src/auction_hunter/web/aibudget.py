"""Loft over hvor mange sprogmodel-kald dashboardet må bruge.

Agenten har haft et loft hele tiden: ``classifier.max_per_run`` findes netop
for at en fejlkonfiguration ikke kan løbe med budgettet. Dashboardet havde
intet tilsvarende, selvom det bruger den samme nøgle. Hvert chat-spørgsmål er
to kald, og knappen på ``/interests?ai=1`` er ét kald pr. klik, så en browser
der genindlæser siden bruger nøglen i det uendelige.

Det er ikke et hul udefra — dashboardet ligger bag ``WEB_PASSWORD`` — men
adgangen er alt-eller-intet, så den der har kodeordet har ubegrænset adgang
til din API-konto. Et loft gør skaden endelig.

Tælleren lever i processen, som login-begrænsningen: uvicorn kører én worker,
og et dashboard til én person har ikke brug for delt tilstand. En genstart
nulstiller den, hvilket er den rigtige afvejning her.

Rammes loftet, opfører siderne sig præcis som uden en nøgle. Den vej er
allerede den veltestede: chatten falder tilbage til en almindelig
nøgleordssøgning, og forslagsknappen viser ingenting. Et loft må ikke være
en ny måde at vælte en side på.
"""

from __future__ import annotations

import logging
import os
import time

from ..classifier import OpenAICompatibleClient

log = logging.getLogger(__name__)

DEFAULT_MAX_PER_HOUR = 60
DEFAULT_WINDOW_SECONDS = 3600


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s er ikke et heltal (%r) — bruger %d", name, raw, default)
        return default
    return max(0, value)


def limit() -> int:
    """Hvor mange kald der må bruges i vinduet. 0 slår webbens AI helt fra."""
    return _env_int("WEB_AI_MAX_PER_HOUR", DEFAULT_MAX_PER_HOUR)


def window_seconds() -> int:
    return _env_int("WEB_AI_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS) or 1


class Budget:
    """Rullende vindue over kald. Tæller tidspunkter, ikke en sum.

    Et fast interval ville lade hele loftet bruges i det sekund vinduet
    skifter, og så igen straks efter. Tidspunkterne gør grænsen jævn.
    """

    def __init__(self) -> None:
        self._calls: list[float] = []

    def _recent(self) -> list[float]:
        cutoff = time.monotonic() - window_seconds()
        self._calls = [t for t in self._calls if t > cutoff]
        return self._calls

    def used(self) -> int:
        return len(self._recent())

    def remaining(self) -> int:
        return max(0, limit() - self.used())

    def allow(self) -> bool:
        return self.remaining() > 0

    def record(self) -> None:
        self._calls.append(time.monotonic())

    def reset(self) -> None:
        """Kun til test."""
        self._calls.clear()


# Én tæller pr. proces. Se modulets docstring for hvorfor det er nok.
BUDGET = Budget()


class BudgetedClient(OpenAICompatibleClient):
    """Klient der tæller sine egne kald med i budgettet.

    Tælles ved kaldet og ikke ved opslaget af klienten, fordi ét
    chat-spørgsmål er to kald. Ville vi kun tælle klienter, ville loftet være
    dobbelt så løst som det ser ud.
    """

    def complete(self, *, system: str, user: str, max_tokens: int = 120) -> str:
        BUDGET.record()
        return super().complete(system=system, user=user, max_tokens=max_tokens)


def status() -> dict[str, int | bool]:
    """Tallene til driftsvisningen."""
    return {
        "used": BUDGET.used(),
        "limit": limit(),
        "remaining": BUDGET.remaining(),
        "window_minutes": window_seconds() // 60,
        "exhausted": not BUDGET.allow(),
    }
