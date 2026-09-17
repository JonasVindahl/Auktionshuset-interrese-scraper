"""Backup og gendannelse af SQLite-hukommelsen.

En rå filkopi af en SQLite-database i WAL-tilstand kan mangle de sidste
transaktioner, og den kan tages midt i en skrivning. Backuppen her bruger
VACUUM INTO, som tager et konsistent oejebliksbillede ogsaa mens agenten
skriver, og som giver praecis én fil uden WAL-soeskende.

Backup indeholder kun databasen. Billederne i data/images/ og
config/interests.yml skal sikres separat; se DEPLOYMENT.md.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """Backup eller gendannelse kunne ikke gennemfoeres."""


def integrity_check(path: str | Path) -> str:
    """SQLites eget svar paa om filen er en intakt database.

    En fil der slet ikke er en database giver en fejl fra SQLite; den vendes
    til en tekst, saa kaldere kan behandle begge dele ens.
    """
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return f"ikke en SQLite-database: {exc}"


def _snapshot(source: Path, target: Path) -> None:
    """Skriv et rent oejebliksbillede til en ny fil.

    VACUUM INTO virker paa en skrivebeskyttet forbindelse og roerer ikke
    kilden, saa den kan koere mens agenten skriver.
    """
    if target.exists():
        raise BackupError(f"findes allerede: {target}")
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


def backup(
    source: str | Path,
    target_dir: str | Path,
    *,
    stamp: str | None = None,
) -> Path:
    """Tag et konsistent backup og returnér stien til filen.

    Filnavnet får et UTC-tidsstempel, så flere backups kan ligge side om side
    uden at overskrive hinanden.
    """
    source = Path(source)
    if not source.exists():
        raise BackupError(f"databasen findes ikke: {source}")

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"{source.stem}-{stamp}.db"

    _snapshot(source, target)

    check = integrity_check(target)
    if check != "ok":
        # Et ugyldigt backup er vaerre end intet backup: det ser ud som om man
        # er daekket ind. Derfor fjernes det, og fejlen meldes.
        target.unlink(missing_ok=True)
        raise BackupError(f"integritetstjek af backup fejlede: {check}")
    return target


def restore(
    source: str | Path,
    target: str | Path,
    *,
    stamp: str | None = None,
) -> Path | None:
    """Gendan target fra source.

    Den nuvaerende database sikres foerst, saa en gendannelse kan rulles
    tilbage. Returnerer stien til sikkerhedskopien, eller None hvis der ikke
    var nogen database i forvejen.
    """
    source = Path(source)
    target = Path(target)
    if not source.exists():
        raise BackupError(f"backupfilen findes ikke: {source}")
    check = integrity_check(source)
    if check != "ok":
        raise BackupError(f"backupfilen er ikke en intakt database: {check}")

    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    safety: Path | None = None
    if target.exists():
        safety = target.with_name(f"{target.name}.foer-gendannelse-{stamp}")
        _snapshot(target, safety)

    # Skriv den nye database ved siden af og flyt den paa plads, saa target
    # aldrig staar halvt overskrevet.
    temp = target.with_name(f".{target.name}.ny-{stamp}")
    temp.unlink(missing_ok=True)
    _snapshot(source, temp)
    os.replace(temp, target)

    # WAL-filerne hoerer til den gamle database og skal ikke overleve en
    # gendannelse, ellers kan de skygge for det netop indlaeste indhold.
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    return safety
