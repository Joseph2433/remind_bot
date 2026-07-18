from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 4

LEGACY_TABLE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS codex_sessions (
        id TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT, name TEXT NOT NULL,
        cwd TEXT NOT NULL, model TEXT, sandbox TEXT NOT NULL, status TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS codex_interactions (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES codex_sessions(id),
        request_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL,
        lark_message_id TEXT, payload_summary TEXT NOT NULL DEFAULT '',
        requested_at TEXT NOT NULL, resolved_at TEXT, expires_at TEXT NOT NULL,
        actor_id TEXT, decision TEXT
    );""",
    """CREATE TABLE IF NOT EXISTS codex_event_dedupe (
        event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT REFERENCES codex_sessions(id),
        interaction_id TEXT REFERENCES codex_interactions(id), notification_type TEXT NOT NULL,
        payload_summary TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL, sent_at TEXT, last_error TEXT, created_at TEXT NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS codex_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT REFERENCES codex_sessions(id),
        interaction_id TEXT REFERENCES codex_interactions(id), event_type TEXT NOT NULL,
        actor_id TEXT, detail_summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    );""",
)

LEGACY_INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_codex_sessions_status ON codex_sessions(status);",
    "CREATE INDEX IF NOT EXISTS idx_codex_interactions_status ON codex_interactions(status);",
    "CREATE INDEX IF NOT EXISTS idx_notification_outbox_due ON notification_outbox(sent_at,next_attempt_at);",
    "CREATE INDEX IF NOT EXISTS idx_codex_audit_session_created ON codex_audit(session_id,created_at,id);",
    "CREATE INDEX IF NOT EXISTS idx_codex_interactions_pending_request ON codex_interactions(request_id) WHERE status='pending';",
)

# These statements mark migrations whose actual execution depends on a
# partially-created legacy database and is therefore coordinated by helpers below.
LEGACY_REQUEST_ID_MIGRATION: tuple[str, ...] = (
    "SELECT json_quote(request_id) AS canonical_request_id FROM codex_interactions;",
)
LEGACY_OUTBOX_MIGRATION: tuple[str, ...] = (
    "ALTER TABLE notification_outbox ADD COLUMN agent TEXT;",
    "ALTER TABLE notification_outbox ADD COLUMN session_name TEXT;",
)

CANONICAL_TABLE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS agent_sessions (
        id TEXT PRIMARY KEY, agent TEXT NOT NULL, name TEXT NOT NULL,
        conversation_id TEXT, turn_id TEXT, cwd TEXT NOT NULL DEFAULT '', model TEXT,
        sandbox TEXT NOT NULL DEFAULT '', permission_mode TEXT, status TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS agent_interactions (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(id),
        request_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        lark_message_id TEXT, payload_summary TEXT NOT NULL DEFAULT '', requested_at TEXT NOT NULL,
        resolved_at TEXT, expires_at TEXT, actor_id TEXT, decision TEXT
    );""",
    """CREATE TABLE IF NOT EXISTS agent_event_dedupe (
        agent TEXT NOT NULL, event_id TEXT NOT NULL, received_at TEXT NOT NULL,
        PRIMARY KEY(agent,event_id)
    );""",
    """CREATE TABLE IF NOT EXISTS agent_notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT REFERENCES agent_sessions(id),
        agent TEXT, session_name TEXT, interaction_id TEXT REFERENCES agent_interactions(id),
        notification_type TEXT NOT NULL, payload_summary TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
        sent_at TEXT, last_error TEXT, created_at TEXT NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS agent_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, session_id TEXT REFERENCES agent_sessions(id),
        interaction_id TEXT REFERENCES agent_interactions(id), event_type TEXT NOT NULL,
        actor_id TEXT, detail_summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    );""",
)

CANONICAL_INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_interactions_pending_request ON agent_interactions(request_id) WHERE status = 'pending';",
    "CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent_status ON agent_sessions(agent, status);",
    "CREATE INDEX IF NOT EXISTS idx_agent_interactions_status ON agent_interactions(status);",
    "CREATE INDEX IF NOT EXISTS idx_agent_outbox_due ON agent_notification_outbox(sent_at, next_attempt_at);",
    "CREATE INDEX IF NOT EXISTS idx_agent_audit_session_created ON agent_audit(session_id, created_at, id);",
)

MIRROR_TRIGGER_STATEMENTS: tuple[str, ...] = (
    """CREATE TRIGGER IF NOT EXISTS trg_agent_dedupe_codex_insert
    AFTER INSERT ON agent_event_dedupe WHEN NEW.agent='codex' BEGIN
      INSERT OR IGNORE INTO codex_event_dedupe(event_id,received_at) VALUES(NEW.event_id,NEW.received_at);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_session_codex_insert
    AFTER INSERT ON agent_sessions WHEN NEW.agent='codex' BEGIN
      INSERT OR IGNORE INTO codex_sessions(id,thread_id,turn_id,name,cwd,model,sandbox,status,summary,created_at,updated_at)
      VALUES(NEW.id,NEW.conversation_id,NEW.turn_id,NEW.name,NEW.cwd,NEW.model,NEW.sandbox,NEW.status,NEW.summary,NEW.created_at,NEW.updated_at);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_session_codex_update
    AFTER UPDATE ON agent_sessions WHEN NEW.agent='codex' BEGIN
      UPDATE codex_sessions SET thread_id=NEW.conversation_id,turn_id=NEW.turn_id,name=NEW.name,cwd=NEW.cwd,model=NEW.model,sandbox=NEW.sandbox,status=NEW.status,summary=NEW.summary,created_at=NEW.created_at,updated_at=NEW.updated_at WHERE id=NEW.id;
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_session_codex_delete
    AFTER DELETE ON agent_sessions WHEN OLD.agent='codex' BEGIN
      DELETE FROM codex_sessions WHERE id=OLD.id;
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_interaction_codex_insert
    AFTER INSERT ON agent_interactions WHEN (SELECT agent FROM agent_sessions WHERE id=NEW.session_id)='codex' BEGIN
      INSERT OR IGNORE INTO codex_interactions(id,session_id,request_id,kind,status,lark_message_id,payload_summary,requested_at,resolved_at,expires_at,actor_id,decision)
      VALUES(NEW.id,NEW.session_id,NEW.request_id,NEW.kind,NEW.status,NEW.lark_message_id,NEW.payload_summary,NEW.requested_at,NEW.resolved_at,COALESCE(NEW.expires_at,NEW.requested_at),NEW.actor_id,NEW.decision);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_interaction_codex_update
    AFTER UPDATE ON agent_interactions WHEN (SELECT agent FROM agent_sessions WHERE id=NEW.session_id)='codex' BEGIN
      UPDATE codex_interactions SET request_id=NEW.request_id,kind=NEW.kind,status=NEW.status,lark_message_id=NEW.lark_message_id,payload_summary=NEW.payload_summary,requested_at=NEW.requested_at,resolved_at=NEW.resolved_at,expires_at=COALESCE(NEW.expires_at,NEW.requested_at),actor_id=NEW.actor_id,decision=NEW.decision WHERE id=NEW.id;
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_interaction_codex_delete
    AFTER DELETE ON agent_interactions WHEN (SELECT agent FROM agent_sessions WHERE id=OLD.session_id)='codex' BEGIN
      DELETE FROM codex_interactions WHERE id=OLD.id;
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_outbox_codex_insert
    AFTER INSERT ON agent_notification_outbox WHEN NEW.agent='codex' BEGIN
      INSERT OR IGNORE INTO notification_outbox(id,session_id,interaction_id,notification_type,payload_summary,attempt_count,next_attempt_at,sent_at,last_error,created_at,agent,session_name)
      VALUES(NEW.id,NEW.session_id,CASE WHEN EXISTS(SELECT 1 FROM codex_interactions WHERE id=NEW.interaction_id) THEN NEW.interaction_id ELSE NULL END,NEW.notification_type,NEW.payload_summary,NEW.attempt_count,NEW.next_attempt_at,NEW.sent_at,NEW.last_error,NEW.created_at,NEW.agent,NEW.session_name);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_outbox_codex_update
    AFTER UPDATE ON agent_notification_outbox WHEN NEW.agent='codex' BEGIN
      UPDATE notification_outbox SET session_id=NEW.session_id,interaction_id=NEW.interaction_id,notification_type=NEW.notification_type,payload_summary=NEW.payload_summary,attempt_count=NEW.attempt_count,next_attempt_at=NEW.next_attempt_at,sent_at=NEW.sent_at,last_error=NEW.last_error,created_at=NEW.created_at,agent=NEW.agent,session_name=NEW.session_name WHERE id=NEW.id;
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_agent_audit_codex_insert
    AFTER INSERT ON agent_audit WHEN (SELECT agent FROM agent_sessions WHERE id=NEW.session_id)='codex' BEGIN
      INSERT OR IGNORE INTO codex_audit(id,session_id,interaction_id,event_type,actor_id,detail_summary,created_at) VALUES(NEW.id,NEW.session_id,NEW.interaction_id,NEW.event_type,NEW.actor_id,NEW.detail_summary,NEW.created_at);
    END;""",
)

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: LEGACY_TABLE_STATEMENTS + LEGACY_INDEX_STATEMENTS,
    2: LEGACY_REQUEST_ID_MIGRATION,
    3: LEGACY_OUTBOX_MIGRATION,
    4: CANONICAL_TABLE_STATEMENTS + CANONICAL_INDEX_STATEMENTS + MIRROR_TRIGGER_STATEMENTS,
}


def _execute_statements(connection: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    for statement in statements:
        connection.execute(statement)


def _legacy_schema(connection: sqlite3.Connection) -> None:
    _execute_statements(connection, MIGRATIONS[1])
    for statement, column in zip(MIGRATIONS[3], ("agent", "session_name")):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(notification_outbox)")}
        if column not in columns:
            connection.execute(statement)


def _canonical_schema(connection: sqlite3.Connection) -> None:
    _execute_statements(connection, CANONICAL_TABLE_STATEMENTS)
    _execute_statements(connection, CANONICAL_INDEX_STATEMENTS)


def _codex_mirror_triggers(connection: sqlite3.Connection) -> None:
    _execute_statements(connection, MIRROR_TRIGGER_STATEMENTS)


def initialize_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"unsupported agent schema version {version}; maximum is {SCHEMA_VERSION}")
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _legacy_schema(connection)
        _canonical_schema(connection)
        if version == 1:
            _execute_statements(connection, MIGRATIONS[2])
        if version < SCHEMA_VERSION:
            request_expr = "json_quote(request_id)" if version == 1 else "request_id"
            connection.execute(
                """INSERT OR IGNORE INTO agent_sessions
                (id,agent,name,conversation_id,turn_id,cwd,model,sandbox,permission_mode,status,summary,created_at,updated_at)
                SELECT id,'codex',name,thread_id,turn_id,cwd,model,sandbox,NULL,status,summary,created_at,updated_at FROM codex_sessions"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO agent_interactions
                (id,session_id,request_id,kind,status,lark_message_id,payload_summary,requested_at,resolved_at,expires_at,actor_id,decision)
                SELECT id,session_id,""" + request_expr + """,kind,status,lark_message_id,payload_summary,requested_at,resolved_at,expires_at,actor_id,decision FROM codex_interactions"""
            )
            connection.execute("INSERT OR IGNORE INTO agent_event_dedupe(agent,event_id,received_at) SELECT 'codex',event_id,received_at FROM codex_event_dedupe")
            connection.execute(
                """INSERT OR IGNORE INTO agent_notification_outbox
                (id,session_id,agent,session_name,interaction_id,notification_type,payload_summary,attempt_count,next_attempt_at,sent_at,last_error,created_at)
                SELECT o.id,o.session_id,'codex',COALESCE(o.session_name,s.name),o.interaction_id,o.notification_type,o.payload_summary,o.attempt_count,o.next_attempt_at,o.sent_at,o.last_error,o.created_at
                FROM notification_outbox o LEFT JOIN codex_sessions s ON s.id=o.session_id"""
            )
            connection.execute("INSERT OR IGNORE INTO agent_audit(id,agent,session_id,interaction_id,event_type,actor_id,detail_summary,created_at) SELECT id,'codex',session_id,interaction_id,event_type,actor_id,detail_summary,created_at FROM codex_audit")
        _codex_mirror_triggers(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
