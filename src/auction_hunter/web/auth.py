"""Adgangskode foran hele dashboardet.

Siden kan redigere interesseprofilen og markere fund, så den skal ikke stå
åben — heller ikke på et hjemmenetværk. Adgangskoden læses som enhver anden
hemmelighed (miljø, ``_FILE`` eller ``_FROM_ENV``), så den kan ligge i en
Docker-secret i stedet for i ``.env``.

Sessionen er en signeret cookie. Serveren gemmer ingen sessionstilstand, så en
genstart af containeren logger dig ud — hvilket er den rigtige afvejning for
et værktøj der ellers kører i månedsvis uden opsyn.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets as _secrets

from ..secrets import SecretError, get_secret

log = logging.getLogger(__name__)

SESSION_KEY = "auth"
# Hvor længe man forbliver logget ind.
SESSION_MAX_AGE = 30 * 24 * 3600


def configured_password() -> str | None:
    """Adgangskoden, hvis en er sat."""
    try:
        return get_secret("WEB_PASSWORD") or None
    except SecretError as exc:
        log.error("WEB_PASSWORD kunne ikke læses: %s", exc)
        return None


def auth_required() -> bool:
    """Om login er slået til.

    Uden adgangskode kører dashboardet åbent. Det logges tydeligt ved opstart,
    så det ikke sker ved et uheld.
    """
    return configured_password() is not None


def check_password(candidate: str) -> bool:
    """Sammenlign i konstant tid, så svaret ikke afslører præfikset."""
    expected = configured_password()
    if expected is None:
        return True
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def session_secret() -> str:
    """Nøglen der signerer session-cookien.

    ``WEB_SECRET_KEY`` kan sættes, så sessioner overlever en genstart. Sker det
    ikke, laves en tilfældig nøgle ved opstart — sikkert, men det logger alle
    ud når containeren genstarter.
    """
    existing = os.environ.get("WEB_SECRET_KEY", "").strip()
    if existing:
        return existing
    log.info(
        "WEB_SECRET_KEY er ikke sat — bruger en tilfældig nøgle. "
        "Sessioner overlever ikke en genstart."
    )
    return _secrets.token_urlsafe(48)
