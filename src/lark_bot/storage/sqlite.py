from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class SQLiteNotificationStore:
    def __init__(self, path: str | Path) -> None:
        self.database = str(path)
        self._memory_connection: sqlite3.Connection | None = None
        if self.database == ":memory:":
            self._memory_connection = sqlite3.connect(self.database)
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def should_send(
        self,
        dedupe_key: str,
        cooldown_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        current = _utc_now(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_sent_at FROM notification_state WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        if row is None:
            return True
        last_sent = datetime.fromisoformat(row[0])
        return (current - last_sent).total_seconds() >= cooldown_seconds

    def record_sent(
        self,
        dedupe_key: str,
        status: str,
        now: datetime | None = None,
    ) -> None:
        current = _utc_now(now).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO notification_state (dedupe_key, status, last_sent_at, send_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    status = excluded.status,
                    last_sent_at = excluded.last_sent_at,
                    send_count = notification_state.send_count + 1
                """,
                (dedupe_key, status, current),
            )
            connection.execute(
                """
                INSERT INTO notification_history (dedupe_key, status, sent_at)
                VALUES (?, ?, ?)
                """,
                (dedupe_key, status, current),
            )

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_state (
                    dedupe_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_sent_at TEXT NOT NULL,
                    send_count INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        return sqlite3.connect(self.path)


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)
