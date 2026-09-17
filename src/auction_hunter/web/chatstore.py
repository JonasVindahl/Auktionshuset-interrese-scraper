"""Samtalehistorik for assistenten, i sin egen databasefil.

Webben maa ikke skrive i agentens hukommelse. Samtalerne ligger derfor i en
separat fil ved siden af, saa en fejl i en chat-rute aldrig kan roere lots,
notifications eller pris-historikken. Det er den samme graense som foer, bare
med et sted mere at laegge sine egne data.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    meta            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

RETENTION_DAYS = 90


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def chat_db_path(db_path: str | Path | None = None) -> Path:
    """Samtalernes fil. Ligger ved siden af agentens database."""
    env = os.environ.get("CHAT_DB_PATH")
    if env:
        return Path(env)
    if db_path and str(db_path) != ":memory:":
        return Path(db_path).parent / "conversations.db"
    return Path("data/conversations.db")


class ChatStore:
    """Samtaler og beskeder. Skriver kun i sin egen fil."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        # Skemaet koeres kun naar det mangler, saa hvert sidekald ikke tager en
        # skrivelås.
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone():
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ChatStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- samtaler ----------------------------------------------------------

    def new_conversation(self, title: str = "") -> int:
        cursor = self.conn.execute(
            "INSERT INTO conversations (created_at, title) VALUES (?,?)",
            (utcnow(), title[:120]),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def latest_conversation(self) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM conversations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row["id"]) if row else None

    def set_title(self, conversation_id: int, title: str) -> None:
        self.conn.execute(
            "UPDATE conversations SET title=? WHERE id=?", (title[:120], conversation_id)
        )
        self.conn.commit()

    def conversations(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- beskeder ----------------------------------------------------------

    def append(
        self, conversation_id: int, role: str, content: str, meta: dict | None = None
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                conversation_id, role, content,
                json.dumps(meta, ensure_ascii=False) if meta else "",
                utcnow(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def messages(
        self, conversation_id: int, *, limit: int | None = None
    ) -> list[sqlite3.Row]:
        if limit:
            return self.conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (conversation_id, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id",
            (conversation_id,),
        ).fetchall()

    def transcript(self, conversation_id: int, limit: int = 6) -> list[dict]:
        """De sidste beskeder, til modellens vindue. Kort og fladt i pris."""
        return [
            {"role": row["role"], "content": row["content"]}
            for row in self.messages(conversation_id, limit=limit)
        ]

    def last_meta(self, conversation_id: int) -> dict:
        row = self.conn.execute(
            "SELECT meta FROM messages WHERE conversation_id=? AND role='assistant' "
            "AND meta <> '' ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["meta"])
        except ValueError:
            return {}

    # -- oprydning ---------------------------------------------------------

    def prune(self, days: int = RETENTION_DAYS) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        cursor = self.conn.execute(
            "DELETE FROM conversations WHERE created_at < ?", (cutoff,)
        )
        self.conn.execute(
            "DELETE FROM messages WHERE conversation_id NOT IN (SELECT id FROM conversations)"
        )
        self.conn.commit()
        return cursor.rowcount

    def maybe_prune(self, days: int = RETENTION_DAYS, *, every_hours: int = 24) -> int:
        """Ryd gamle samtaler, men hoejst en gang i doegnet.

        Kaldes efter en skrivning, saa en GET ikke aabner en skrivetransaktion.
        """
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone():
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self.conn.commit()
        last = self.conn.execute("SELECT value FROM meta WHERE key='pruned_at'").fetchone()
        if last:
            try:
                elapsed = datetime.now(UTC) - datetime.fromisoformat(last["value"])
                if elapsed.total_seconds() < every_hours * 3600:
                    return 0
            except ValueError:
                pass
        removed = self.prune(days)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('pruned_at', ?)", (utcnow(),)
        )
        self.conn.commit()
        return removed
