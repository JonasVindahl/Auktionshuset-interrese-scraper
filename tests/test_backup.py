"""Backup og gendannelse af hele hukommelsen."""

from __future__ import annotations

import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from auction_hunter.backup import (
    BackupError,
    backup,
    integrity_check,
    restore,
)
from auction_hunter.storage import Store


@pytest.fixture(autouse=True)
def _ingen_chat_sti(monkeypatch):
    # chat_db_path slår CHAT_DB_PATH op først; uden dette ville testene skrive
    # samtaler et tilfældigt sted.
    monkeypatch.delenv("CHAT_DB_PATH", raising=False)


def _db(path: Path, rows: int, table: str = "notifications") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
    for i in range(rows):
        conn.execute(f"INSERT INTO {table} VALUES (?)", (f"L{i}",))
    conn.commit()
    conn.close()
    return path


def _count(path: Path, table: str = "notifications") -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _full_tree(tmp_path: Path) -> Path:
    db = _db(tmp_path / "data" / "hunter.db", 5)
    _db(tmp_path / "data" / "conversations.db", 2, table="conversations")
    images = tmp_path / "data" / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "abc.jpg").write_bytes(b"billede-et")
    (images / "def.jpg").write_bytes(b"billede-to")
    return db


# -- backup ----------------------------------------------------------------

def test_backup_indeholder_database_samtaler_og_billeder(tmp_path):
    db = _full_tree(tmp_path)
    result = backup(db, tmp_path / "backups", stamp="fast")

    assert result.path.name == "hunter-fast.tar.gz"
    assert result.has_conversations is True
    assert result.image_count == 2

    with tarfile.open(result.path) as tar:
        names = sorted(tar.getnames())
    assert "db/main.db" in names
    assert "db/conversations.db" in names
    assert "images/abc.jpg" in names
    assert "images/def.jpg" in names


def test_backup_uden_samtaler_og_billeder_har_kun_databasen(tmp_path):
    db = _db(tmp_path / "hunter.db", 1)
    result = backup(db, tmp_path / "b", stamp="fast")
    assert result.has_conversations is False
    assert result.image_count == 0

    with tarfile.open(result.path) as tar:
        assert tar.getnames() == ["db/main.db"]


def test_databasen_i_arkivet_er_intakt(tmp_path):
    db = _full_tree(tmp_path)
    result = backup(db, tmp_path / "b", stamp="fast")
    with tarfile.open(result.path) as tar:
        data = tar.extractfile("db/main.db").read()
    assert data.startswith(b"SQLite format 3")


def test_backup_af_manglende_database_fe_jler(tmp_path):
    with pytest.raises(BackupError):
        backup(tmp_path / "findes-ikke.db", tmp_path / "b")


def test_backup_overskriver_ikke_et_eksisterende_navn(tmp_path):
    db = _db(tmp_path / "hunter.db", 1)
    backup(db, tmp_path / "b", stamp="fast")
    with pytest.raises(BackupError):
        backup(db, tmp_path / "b", stamp="fast")


# -- gendannelse -----------------------------------------------------------

def test_gendannelse_af_arkiv_giver_alt_tilbage(tmp_path):
    db = _full_tree(tmp_path)
    result = backup(db, tmp_path / "b", stamp="fast")

    fresh = tmp_path / "ny" / "hunter.db"
    restored = restore(result.path, fresh, stamp="nu")

    assert _count(fresh) == 5
    assert (tmp_path / "ny" / "conversations.db").exists()
    assert _count(tmp_path / "ny" / "conversations.db", "conversations") == 2
    images = tmp_path / "ny" / "images"
    assert sorted(p.name for p in images.iterdir()) == ["abc.jpg", "def.jpg"]
    assert restored.image_count == 2
    assert restored.has_conversations is True
    assert restored.safety == ()


def test_gendannelse_sikrer_det_gamle(tmp_path):
    db = _full_tree(tmp_path)
    result = backup(db, tmp_path / "b", stamp="fast")

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO notifications VALUES ('NY')")
    conn.commit()
    conn.close()
    (tmp_path / "data" / "images" / "nyt.jpg").write_bytes(b"nyt")

    restored = restore(result.path, db, stamp="nu")

    assert _count(db) == 5
    assert len(restored.safety) == 3
    assert all(path.exists() for path in restored.safety)
    billedsikker = [p for p in restored.safety if p.name.startswith("images")][0]
    assert (billedsikker / "nyt.jpg").exists()


def test_gendannelse_af_enkel_databasefil_virker_stadig(tmp_path):
    db = _db(tmp_path / "hunter.db", 5)
    enkel = _db(tmp_path / "enkel.db", 1)

    result = restore(enkel, db, stamp="nu")
    assert _count(db) == 1
    assert result.safety and result.safety[0].exists()


def test_gendannelse_afviser_skrald(tmp_path):
    bogus = tmp_path / "skrald.db"
    bogus.write_text("dette er ikke en database", encoding="utf-8")
    with pytest.raises(BackupError):
        restore(bogus, tmp_path / "hunter.db")


def test_gendannelse_afviser_arkiv_uden_database(tmp_path):
    archive = tmp_path / "tom.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("laesmig.txt")
        payload = b"ingen database her"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(BackupError):
        restore(archive, tmp_path / "hunter.db")


def test_gendannelse_afviser_sti_uden_for_maalet(tmp_path):
    archive = tmp_path / "ondsindet.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../udenfor.txt")
        payload = b"nej"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(BackupError):
        restore(archive, tmp_path / "hunter.db")


def test_integritetstjek_paa_skrald_giver_tekst(tmp_path):
    bogus = tmp_path / "skrald.db"
    bogus.write_text("ikke en database", encoding="utf-8")
    check = integrity_check(bogus)
    assert check != "ok"
    assert "ikke en SQLite-database" in check


def test_restore_afviser_mens_agenten_koerer(tmp_path):
    """Gendannelse under en aaben agent taber hele gendannelsen i stilhed.

    Agenten beholder den gamle inode og skriver videre i en slettet fil.
    --yes var indtil nu den eneste kontrol, og den er en afkrydsning.
    """
    db = tmp_path / "hunter.db"
    with Store(db) as store:
        store.start_run()
        arkiv = backup(db, tmp_path / "ud").path
        # Store har et friskt livstegn saa laenge den er aaben.
        with pytest.raises(BackupError, match="ser ud til at koere"):
            restore(arkiv, db)

    # Efter close() er livstegnet vaek, og gendannelsen kan gennemfoeres.
    restore(arkiv, db)


def test_restore_kan_tvinges_forbi_et_efterladt_livstegn(tmp_path):
    """Et livstegn fra en proces der doede maa kunne tilsidesaettes bevidst."""
    db = tmp_path / "hunter.db"
    with Store(db) as store:
        store.start_run()
        arkiv = backup(db, tmp_path / "ud").path

    # Efterlad et friskt livstegn som en doed proces ville have gjort.
    Path(str(db) + ".live").write_text("999999 nu\n", encoding="utf-8")
    with pytest.raises(BackupError, match="ser ud til at koere"):
        restore(arkiv, db)
    restore(arkiv, db, force=True)
