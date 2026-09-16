"""Opløsning af hemmeligheder fra miljøvariabler eller filer.

Understøtter Docker-secrets, hvor værdien lever i en fil i stedet for i en
miljøvariabel. Rækkefølgen er:

1. ``<NAVN>_FILE`` — sti til en fil med værdien (Docker secret-stil)
2. ``<NAVN>`` — værdien direkte i miljøet
3. ``<NAVN>_FROM_ENV`` — navnet på en anden miljøvariabel der indeholder værdien

Så en webhook kan sættes på den måde der passer til miljøet, uden at koden
skal genbygges.
"""

from __future__ import annotations

import os
from pathlib import Path


class SecretError(RuntimeError):
    """En påkrævet hemmelighed mangler eller kunne ikke læses."""


def _read_file(path: str, name: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SecretError(f"Kunne ikke læse {name}_FILE ({path}): {exc}") from exc
    if not value:
        raise SecretError(f"Filen i {name}_FILE ({path}) er tom")
    return value


def get_secret(name: str, *, required: bool = False, default: str | None = None) -> str | None:
    """Hent en hemmelighed efter den dokumenterede rækkefølge.

    Rækkefølgen gør det muligt at pege på en fil (Docker secrets), en direkte
    miljøvariabel, eller indirekte via en anden variabels navn.
    """
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return _read_file(file_path.strip(), name)

    indirect = os.environ.get(f"{name}_FROM_ENV")
    if indirect:
        value = os.environ.get(indirect.strip())
        if value and value.strip():
            return value.strip()
        raise SecretError(
            f"{name}_FROM_ENV peger på '{indirect}', men den miljøvariabel er tom eller findes ikke"
        )

    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()

    if required:
        raise SecretError(
            f"Mangler {name}. Sæt enten {name}, {name}_FILE (fil med værdien) "
            f"eller {name}_FROM_ENV (navn på anden variabel)."
        )
    return default


def get_config_value(name: str, *, default=None, required: bool = False):
    """Som get_secret, men uden at kræve en hemmelighed."""
    return get_secret(name, required=required, default=default)


def redact(value: str | None) -> str:
    """Maskér en hemmelighed til logning, så kun et fingerprint vises."""
    if not value:
        return "<ikke sat>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]} (len={len(value)})"
