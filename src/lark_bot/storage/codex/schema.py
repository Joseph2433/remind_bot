from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 2

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
                CREATE TABLE IF NOT EXISTS codex_sessions (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    turn_id TEXT,
                    name TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    model TEXT,
                    sandbox TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
        """,
        """
                CREATE TABLE IF NOT EXISTS codex_interactions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES codex_sessions(id),
                    request_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lark_message_id TEXT,
                    payload_summary TEXT NOT NULL DEFAULT '',
                    requested_at TEXT NOT NULL,
                    resolved_at TEXT,
                    expires_at TEXT NOT NULL,
                    actor_id TEXT,
                    decision TEXT
                )
        """,
        """
                CREATE TABLE IF NOT EXISTS codex_event_dedupe (
                    event_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL
                )
        """,
        """
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT REFERENCES codex_sessions(id),
                    interaction_id TEXT REFERENCES codex_interactions(id),
                    notification_type TEXT NOT NULL,
                    payload_summary TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    sent_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL
                )
        """,
        """
                CREATE TABLE IF NOT EXISTS codex_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT REFERENCES codex_sessions(id),
                    interaction_id TEXT REFERENCES codex_interactions(id),
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    detail_summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
        """,
        "CREATE INDEX IF NOT EXISTS idx_codex_sessions_status "
        "ON codex_sessions(status)",
        "CREATE INDEX IF NOT EXISTS idx_codex_interactions_status "
        "ON codex_interactions(status)",
        "CREATE INDEX IF NOT EXISTS idx_notification_outbox_due "
        "ON notification_outbox(sent_at, next_attempt_at)",
        "CREATE INDEX IF NOT EXISTS idx_codex_audit_session_created "
        "ON codex_audit(session_id, created_at, id)",
    ),
    2: (
        """
        CREATE TABLE codex_interactions_v2 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES codex_sessions(id),
            request_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            lark_message_id TEXT,
            payload_summary TEXT NOT NULL DEFAULT '',
            requested_at TEXT NOT NULL,
            resolved_at TEXT,
            expires_at TEXT NOT NULL,
            actor_id TEXT,
            decision TEXT
        )
        """,
        """
        INSERT INTO codex_interactions_v2 (
            id, session_id, request_id, kind, status, lark_message_id,
            payload_summary, requested_at, resolved_at, expires_at, actor_id, decision
        )
        SELECT id, session_id, json_quote(request_id), kind, status, lark_message_id,
               payload_summary, requested_at, resolved_at, expires_at, actor_id, decision
        FROM codex_interactions
        """,
        "DROP TABLE codex_interactions",
        "ALTER TABLE codex_interactions_v2 RENAME TO codex_interactions",
        "CREATE INDEX idx_codex_interactions_status ON codex_interactions(status)",
        "CREATE UNIQUE INDEX idx_codex_interactions_pending_request "
        "ON codex_interactions(request_id) WHERE status = 'pending'",
    ),
}


def initialize_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported Codex schema version {version}; maximum is {SCHEMA_VERSION}"
        )
    if version == SCHEMA_VERSION:
        return

    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    for target_version in range(version + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS[target_version]:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {target_version}")
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
