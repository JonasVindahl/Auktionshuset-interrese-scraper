"""Backup og gendannelse af SQLite-hukommelsen."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from auction_hunter.backup import BackupError, backup, integrity_check, restore


def _db(path: Path, rows: int) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE notifications (lot_id TEXT PRIMARY KEY, category_key TEXT)"
    )
    for i in range(rows):
        conn.execute("INSERT INTO notifications VALUES (?,?)", (f"L{i}", "it_tech"))
    conn.commit()
    conn.close()
    return path


def _count(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    finally:
        conn.close()


def test_backup_giver_en_intakt_enkeltfil(tmp_path):
    source = _db(tmp_path / "hunter.db", 3)
    target = backup(source, tmp_path / "backups", stamp="20260101T000000Z")

    assert target.name == "hunter-20260101T000000Z.db"
    assert integrity_check(target) == "ok"
    # VACUUM INTO giver praecis én fil, uden WAL-soeskende.
    assert not Path(str(target) + "-wal").exists()
    assert not Path(str(target) + "-shm").exists()
    assert _count(target) == 3


def test_backup_af_manglende_database_fe_jler(tmp_path):
    with pytest.raises(BackupError):
        backup(tmp_path / "findes-ikke.db", tmp_path / "backups")


def test_backup_overskriver_ikke_et_eksisterende_navn(tmp_path):
    source = _db(tmp_path / "hunter.db", 1)
    backup(source, tmp_path / "b", stamp="fast")
    with pytest.raises(BackupError):
        backup(source, tmp_path / "b", stamp="fast")


def test_restore_gendanner_indhold_og_sikrer_det_gamle(tmp_path):
    source = _db(tmp_path / "hunter.db", 5)
    snapshot = backup(source, tmp_path / "b", stamp="fast")

    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO notifications VALUES ('NY','it_tech')")
    conn.commit()
    conn.close()
    assert _count(source) == 6

    safety = restore(snapshot, source, stamp="nu")
    assert safety is not None and safety.exists()
    assert _count(source) == 5
    # Sikkerhedskopien indeholder den version der blev erstattet.
    assert _count(safety) == 6


def test_restore_uden_eksisterende_database_giver_ingen_sikkerhedskopi(tmp_path):
    source = _db(tmp_path / "hunter.db", 2)
    snapshot = backup(source, tmp_path / "b", stamp="fast")
    target = tmp_path / "ny.db"
    assert restore(snapshot, target, stamp="nu") is None
    assert _count(target) == 2


def test_restore_afviser_en_fil_der_ikke_er_en_database(tmp_path):
    bogus = tmp_path / "skrald.db"
    bogus.write_text("dette er ikke en database", encoding="utf-8")
    with pytest.raises(BackupError):
        restore(bogus, tmp_path / "hunter.db")


def test_integritetstjek_paa_skrald_giver_tekst(tmp_path):
    bogus = tmp_path / "skrald.db"
    bogus.write_text("ikke en database", encoding="utf-8")
    check = integrity_check(bogus)
    assert check != "ok"
    assert "ikke en SQLite-database" in check
