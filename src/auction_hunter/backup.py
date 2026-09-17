"""Backup og gendannelse af hele hukommelsen.

Et backup er én tar.gz med:

    db/main.db            konsistent snapshot af agentens database
    db/conversations.db   samtalernes database, hvis den findes
    images/               de cachede miniaturebilleder, hvis de findes

Snapshotet tages med VACUUM INTO. En rå filkopi af en SQLite-database i
WAL-tilstand kan mangle de sidste transaktioner og kan tages midt i en
skrivning; VACUUM INTO giver et konsistent oejebliksbillede ogsaa mens agenten
skriver, og giver én ren fil uden WAL-soeskende.

config/interests.yml er ikke med. Den ligger i git, og en gendannelse skal ikke
kunne rulle profilændringer tilbage ved et uheld.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .storage import HEARTBEAT_SUFFIX
from .web.chatstore import chat_db_path

log = logging.getLogger(__name__)

# Hvor gammelt et livstegn skal vaere foer databasen regnes for ledig.
# To scrape-intervaller: en agent der koerer, roerer filen hvert 15. minut.
HEARTBEAT_STALE_SECONDS = 30 * 60

ARCHIVE_SUFFIX = ".tar.gz"
DB_MEMBER = "db/main.db"
CHAT_MEMBER = "db/conversations.db"
IMAGES_MEMBER = "images"


class BackupError(RuntimeError):
    """Backup eller gendannelse kunne ikke gennemfoeres."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    has_conversations: bool
    image_count: int


@dataclass(frozen=True)
class RestoreResult:
    safety: tuple[Path, ...]
    has_conversations: bool
    image_count: int


def _stamp(value: str | None) -> str:
    return value or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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


def _drop_wal(target: Path) -> None:
    """WAL-filerne hoerer til den gamle database og maa ikke overleve."""
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Pak arkivet ud, men afvis links og stier uden for maalmappen."""
    root = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise BackupError(f"arkivmedlemmet {member.name} er et link")
        resolved = (root / member.name).resolve()
        if resolved != root and root not in resolved.parents:
            raise BackupError(f"arkivmedlemmet {member.name} peger uden for maalet")
    try:
        tar.extractall(dest, filter="data")   # Python 3.12+
    except TypeError:
        tar.extractall(dest)


def _count_files(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.iterdir() if path.is_file())


def backup(
    source: str | Path,
    target_dir: str | Path,
    *,
    stamp: str | None = None,
) -> BackupResult:
    """Tag et komplet backup og returnér hvad det indeholder."""
    source = Path(source)
    if not source.exists():
        raise BackupError(f"databasen findes ikke: {source}")

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(stamp)
    target = target_dir / f"{source.stem}-{stamp}{ARCHIVE_SUFFIX}"
    if target.exists():
        raise BackupError(f"der findes allerede et backup med samme navn: {target}")

    conversations = chat_db_path(source)
    have_conversations = conversations.exists() and conversations != source
    images = source.parent / IMAGES_MEMBER
    image_count = _count_files(images)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        main_snapshot = tmp / "main.db"
        _snapshot(source, main_snapshot)
        check = integrity_check(main_snapshot)
        if check != "ok":
            # Et ugyldigt backup er vaerre end intet backup: det ser ud ud som
            # om man er daekket ind. Derfor skrives arkivet slet ikke.
            raise BackupError(f"integritetstjek af snapshot fejlede: {check}")

        chat_snapshot: Path | None = None
        if have_conversations:
            chat_snapshot = tmp / "conversations.db"
            _snapshot(conversations, chat_snapshot)

        with tarfile.open(target, "w:gz") as tar:
            tar.add(main_snapshot, arcname=DB_MEMBER)
            if chat_snapshot is not None:
                tar.add(chat_snapshot, arcname=CHAT_MEMBER)
            if image_count:
                tar.add(images, arcname=IMAGES_MEMBER)

    return BackupResult(target, have_conversations, image_count)


def agent_heartbeat_age(target: str | Path) -> float | None:
    """Sekunder siden en agent sidst roerte databasen, eller None.

    Store skriver et livstegn ved siden af databasen ved opstart og ved hver
    koersel. Filens alder er det signal der virker paa tvaers af containere,
    hvor et PID-tjek ikke goer: agenten og gendannelsen har hver sit PID-rum,
    men deler mappen.
    """
    beat = Path(str(target) + HEARTBEAT_SUFFIX)
    try:
        return max(0.0, time.time() - beat.stat().st_mtime)
    except OSError:
        return None


def _refuse_if_agent_is_live(target: Path, *, force: bool) -> None:
    """Afvis en gendannelse mens agenten aabenbart koerer.

    Gendannelse bytter filen ud under en aaben forbindelse. Goer man det mens
    agenten koerer, beholder den den gamle inode og skriver videre i en
    slettet fil, saa hele gendannelsen forsvinder uden en fejl.

    Signalet er ikke perfekt: et livstegn kan vaere efterladt af en proces der
    er doed uden at rydde op, og en agent der har vaeret stoppet i over en halv
    time ser ledig ud. Derfor kan det tilsidesaettes, men bevidst.
    """
    age = agent_heartbeat_age(target)
    if age is None or age > HEARTBEAT_STALE_SECONDS or force:
        if age is not None and age <= HEARTBEAT_STALE_SECONDS and force:
            log.warning("Gendanner selvom agenten ser ud til at koere (--force)")
        return
    raise BackupError(
        f"agenten ser ud til at koere: {target}{HEARTBEAT_SUFFIX} blev roert "
        f"for {int(age)} sekunder siden. Stop 'hunter' og 'web' foerst, ellers "
        f"skriver den videre i den gamle fil og gendannelsen gaar tabt. "
        f"Brug --force hvis du ved at livstegnet er efterladt."
    )


def restore(
    source: str | Path,
    target: str | Path,
    *,
    stamp: str | None = None,
    force: bool = False,
) -> RestoreResult:
    """Gendan target fra et backup.

    Arkivet sikres foerst, saa en gendannelse kan rulles tilbage. Returnerer
    stierne til sikkerhedskopierne, ikke selve indholdet.
    """
    source = Path(source)
    target = Path(target)
    if not source.exists():
        raise BackupError(f"backupfilen findes ikke: {source}")
    _refuse_if_agent_is_live(target, force=force)

    stamp = _stamp(stamp)
    if tarfile.is_tarfile(source):
        return _restore_archive(source, target, stamp)
    return _restore_db(source, target, stamp)


def _restore_db(source: Path, target: Path, stamp: str) -> RestoreResult:
    check = integrity_check(source)
    if check != "ok":
        raise BackupError(f"backupfilen er ikke en intakt database: {check}")

    target.parent.mkdir(parents=True, exist_ok=True)
    safety: list[Path] = []
    if target.exists():
        dest = target.with_name(f"{target.name}.foer-gendannelse-{stamp}")
        _snapshot(target, dest)
        safety.append(dest)

    _replace_db(source, target, stamp)
    return RestoreResult(tuple(safety), False, 0)


def _replace_db(source: Path, target: Path, stamp: str) -> None:
    """Skriv den nye database ved siden af og flyt den paa plads."""
    temp = target.with_name(f".{target.name}.ny-{stamp}")
    temp.unlink(missing_ok=True)
    _snapshot(source, temp)
    os.replace(temp, target)
    _drop_wal(target)


def _restore_archive(source: Path, target: Path, stamp: str) -> RestoreResult:
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        with tarfile.open(source, "r:gz") as tar:
            _safe_extract(tar, tmp)

        main = tmp / DB_MEMBER
        if not main.exists():
            raise BackupError(f"arkivet mangler {DB_MEMBER}")
        check = integrity_check(main)
        if check != "ok":
            raise BackupError(f"databasen i arkivet er ikke intakt: {check}")

        safety: list[Path] = []
        if target.exists():
            dest = target.with_name(f"{target.name}.foer-gendannelse-{stamp}")
            _snapshot(target, dest)
            safety.append(dest)
        _replace_db(main, target, stamp)

        chat_source = tmp / CHAT_MEMBER
        have_conversations = chat_source.exists()
        if have_conversations:
            chat_target = chat_db_path(target)
            if chat_target.exists():
                dest = chat_target.with_name(
                    f"{chat_target.name}.foer-gendannelse-{stamp}"
                )
                _snapshot(chat_target, dest)
                safety.append(dest)
            _replace_db(chat_source, chat_target, stamp)

        images_source = tmp / IMAGES_MEMBER
        image_count = _count_files(images_source)
        if image_count:
            images_target = target.parent / IMAGES_MEMBER
            if _count_files(images_target):
                dest = images_target.with_name(f"images.foer-gendannelse-{stamp}")
                if dest.exists():
                    raise BackupError(
                        f"der findes allerede en sikkerhedskopi af billederne: {dest}"
                    )
                images_target.rename(dest)
                safety.append(dest)
            shutil.copytree(images_source, images_target, dirs_exist_ok=True)

    return RestoreResult(tuple(safety), have_conversations, image_count)
