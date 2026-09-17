"""Tests for samtalehistorikken.

Den ligger i sin egen fil, saa webben aldrig skriver i agentens database. Det
er den graense testene vogter her.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from auction_hunter.web.chatstore import ChatStore, chat_db_path, utcnow


def test_samtalen_runder_tur(tmp_path):
    with ChatStore(tmp_path / "c.db") as store:
        cid = store.new_conversation("foerste")
        store.append(cid, "user", "hej")
        store.append(cid, "assistant", "svar", {"selected": ["a1"]})

        assert [row["role"] for row in store.messages(cid)] == ["user", "assistant"]
        assert store.transcript(cid, limit=1) == [{"role": "assistant", "content": "svar"}]
        assert store.last_meta(cid) == {"selected": ["a1"]}
        assert store.latest_conversation() == cid


def test_ny_samtale_er_en_ny_raekke(tmp_path):
    with ChatStore(tmp_path / "c.db") as store:
        first = store.new_conversation()
        second = store.new_conversation()
        assert first != second
        assert store.latest_conversation() == second
        assert len(store.conversations()) == 2


def test_vinduet_tager_de_sidste(tmp_path):
    with ChatStore(tmp_path / "c.db") as store:
        cid = store.new_conversation()
        for i in range(10):
            store.append(cid, "user", f"besked {i}")
        window = store.transcript(cid, limit=3)
        assert [m["content"] for m in window] == ["besked 7", "besked 8", "besked 9"]


def test_titel_kan_saettes(tmp_path):
    with ChatStore(tmp_path / "c.db") as store:
        cid = store.new_conversation()
        store.set_title(cid, "om NAS")
        assert store.conversations()[0]["title"] == "om NAS"


def test_oprydning_fjerner_gamle_samtaler(tmp_path):
    with ChatStore(tmp_path / "c.db") as store:
        cid = store.new_conversation()
        store.append(cid, "user", "gammel")
        old = (datetime.now(UTC) - timedelta(days=200)).isoformat(timespec="seconds")
        store.conn.execute("UPDATE conversations SET created_at=?", (old,))
        store.conn.commit()
        assert store.prune(days=90) == 1
        assert store.messages(cid) == []


def test_tom_meta_giver_tom_dict(tmp_path):
    with ChatStore(tmp_path / "c.db") as store:
        cid = store.new_conversation()
        store.append(cid, "assistant", "uden meta")
        assert store.last_meta(cid) == {}


def test_stien_ligger_ved_siden_af_databasen(tmp_path):
    assert (
        chat_db_path(tmp_path / "data" / "hunter.db")
        == tmp_path / "data" / "conversations.db"
    )


def test_stien_kan_overstyres(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "egne.db"))
    assert chat_db_path("ignoreret.db") == tmp_path / "egne.db"


def test_utcnow_er_iso():
    assert utcnow().startswith("20")


def test_oprydning_koerer_hoejst_en_gang_i_doegnet(tmp_path):
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat(timespec="seconds")
    with ChatStore(tmp_path / "c.db") as store:
        first = store.new_conversation()
        store.conn.execute("UPDATE conversations SET created_at=? WHERE id=?", (old, first))
        store.conn.commit()
        assert store.maybe_prune(days=90) == 1

        # En ny gammel samtale skal ligge, fordi oprydningen lige har koert.
        second = store.new_conversation()
        store.conn.execute("UPDATE conversations SET created_at=? WHERE id=?", (old, second))
        store.conn.commit()
        assert store.maybe_prune(days=90) == 0
        assert len(store.conversations()) == 1
