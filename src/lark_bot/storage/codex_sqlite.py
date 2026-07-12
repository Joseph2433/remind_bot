from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Iterator, Self

from lark_bot.codex_models import (
    CodexAuditEntry,
    CodexSession,
    InteractionDecision,
    InteractionKind,
    InteractionStatus,
    NotificationOutboxItem,
    PendingInteraction,
    SessionStatus,
    StartupReconciliationResult,
)
from lark_bot.redaction import redact_text


_UNSET = object()
_SUMMARY_LIMIT = 2000
_SCHEMA_VERSION = 2
_ACTIVE_SESSION_STATUSES = (
    SessionStatus.STARTING,
    SessionStatus.RUNNING,
    SessionStatus.WAITING_FOR_APPROVAL,
    SessionStatus.WAITING_FOR_INPUT,
)
_TERMINAL_SESSION_STATUSES = (
    SessionStatus.SUCCEEDED,
    SessionStatus.FAILED,
    SessionStatus.INTERRUPTED,
    SessionStatus.CANCELLED,
)
_ALLOWED_DECISIONS = {
    InteractionKind.EXEC_APPROVAL: frozenset(
        (InteractionDecision.APPROVED, InteractionDecision.DENIED)
    ),
    InteractionKind.FILE_CHANGE_APPROVAL: frozenset(
        (InteractionDecision.ACCEPT, InteractionDecision.DECLINE)
    ),
    InteractionKind.PERMISSION_REQUEST: frozenset(
        (InteractionDecision.GRANTED, InteractionDecision.DENIED)
    ),
}


class SQLiteCodexStore:
    def __init__(self, path: str | Path) -> None:
        self.database = str(path)
        self._closed = False
        self._memory_connection: sqlite3.Connection | None = None
        if self.database == ":memory:":
            self._memory_connection = self._new_connection(self.database)
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def create_session(self, session: CodexSession) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO codex_sessions (
                    id, thread_id, turn_id, name, cwd, model, sandbox, status,
                    summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.thread_id,
                    session.turn_id,
                    session.name,
                    session.cwd,
                    session.model,
                    session.sandbox,
                    session.status.value,
                    _safe_summary(session.summary),
                    _serialize_datetime(session.created_at),
                    _serialize_datetime(session.updated_at),
                ),
            )

    def get_session(self, session_id: str) -> CodexSession | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM codex_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def get_session_by_thread(self, thread_id: str) -> CodexSession | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM codex_sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def list_sessions(
        self,
        status: SessionStatus | None = None,
    ) -> list[CodexSession]:
        query = "SELECT * FROM codex_sessions"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status.value,)
        query += " ORDER BY created_at, id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_session_from_row(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        *,
        status: SessionStatus | None = None,
        thread_id: str | None | object = _UNSET,
        turn_id: str | None | object = _UNSET,
        summary: str | None = None,
        updated_at: datetime | None = None,
    ) -> CodexSession | None:
        assignments: list[str] = []
        parameters: list[object] = []
        if status is not None:
            assignments.append("status = ?")
            parameters.append(status.value)
        if thread_id is not _UNSET:
            assignments.append("thread_id = ?")
            parameters.append(thread_id)
        if turn_id is not _UNSET:
            assignments.append("turn_id = ?")
            parameters.append(turn_id)
        if summary is not None:
            assignments.append("summary = ?")
            parameters.append(_safe_summary(summary))
        if not assignments:
            return self.get_session(session_id)
        assignments.append("updated_at = ?")
        parameters.append(_serialize_datetime(updated_at or datetime.now(timezone.utc)))
        parameters.append(session_id)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE codex_sessions SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
        return self.get_session(session_id)

    def update_session_if_status(
        self,
        session_id: str,
        expected_statuses: tuple[SessionStatus, ...],
        *,
        status: SessionStatus,
        thread_id: str | None | object = _UNSET,
        turn_id: str | None | object = _UNSET,
        summary: str | None = None,
        updated_at: datetime | None = None,
    ) -> bool:
        normalized_expected = tuple(SessionStatus(value) for value in expected_statuses)
        if not normalized_expected:
            return False
        assignments = ["status = ?"]
        parameters: list[object] = [SessionStatus(status).value]
        if thread_id is not _UNSET:
            assignments.append("thread_id = ?")
            parameters.append(thread_id)
        if turn_id is not _UNSET:
            assignments.append("turn_id = ?")
            parameters.append(turn_id)
        if summary is not None:
            assignments.append("summary = ?")
            parameters.append(_safe_summary(summary))
        assignments.append("updated_at = ?")
        parameters.append(_serialize_datetime(updated_at or datetime.now(timezone.utc)))
        placeholders = ", ".join("?" for _ in normalized_expected)
        parameters.extend((session_id, *(value.value for value in normalized_expected)))
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE codex_sessions SET {', '.join(assignments)}
                WHERE id = ? AND status IN ({placeholders})
                """,
                parameters,
            )
        return cursor.rowcount == 1

    def create_interaction(self, interaction: PendingInteraction) -> None:
        interaction_status = InteractionStatus(interaction.status)
        if interaction_status is InteractionStatus.PENDING and any(
            value is not None
            for value in (
                interaction.resolved_at,
                interaction.actor_id,
                interaction.decision,
            )
        ):
            raise ValueError("pending interaction cannot contain resolution metadata")
        interaction = PendingInteraction.model_validate(interaction.model_dump())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO codex_interactions (
                    id, session_id, request_id, kind, status, lark_message_id,
                    payload_summary, requested_at, resolved_at, expires_at,
                    actor_id, decision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.id,
                    interaction.session_id,
                    interaction.request_id,
                    interaction.kind.value,
                    interaction.status.value,
                    interaction.lark_message_id,
                    _safe_summary(interaction.payload_summary),
                    _serialize_datetime(interaction.requested_at),
                    _serialize_optional_datetime(interaction.resolved_at),
                    _serialize_datetime(interaction.expires_at),
                    interaction.actor_id,
                    (
                        interaction.decision.value
                        if interaction.decision is not None
                        else None
                    ),
                ),
            )

    def create_interaction_and_mark_waiting(
        self,
        interaction: PendingInteraction,
        waiting_status: SessionStatus,
        updated_at: datetime,
    ) -> bool:
        waiting_status = SessionStatus(waiting_status)
        if waiting_status not in {
            SessionStatus.WAITING_FOR_APPROVAL,
            SessionStatus.WAITING_FOR_INPUT,
        }:
            raise ValueError("waiting_status must be a waiting session status")
        interaction = PendingInteraction.model_validate(interaction.model_dump())
        active_values = tuple(status.value for status in _ACTIVE_SESSION_STATUSES)
        placeholders = ", ".join("?" for _ in active_values)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    f"""
                    UPDATE codex_sessions SET status = ?, updated_at = ?
                    WHERE id = ? AND status IN ({placeholders})
                    """,
                    (
                        waiting_status.value,
                        _serialize_datetime(updated_at),
                        interaction.session_id,
                        *active_values,
                    ),
                )
                if cursor.rowcount != 1:
                    return False
                connection.execute(
                    """
                    INSERT INTO codex_interactions (
                        id, session_id, request_id, kind, status, lark_message_id,
                        payload_summary, requested_at, resolved_at, expires_at,
                        actor_id, decision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _interaction_values(interaction),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_interaction(self, interaction_id: str) -> PendingInteraction | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM codex_interactions WHERE id = ?",
                (interaction_id,),
            ).fetchone()
        return _interaction_from_row(row) if row is not None else None

    def get_pending_interaction(
        self,
        request_id: str,
    ) -> PendingInteraction | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM codex_interactions
                WHERE request_id = ? AND status = ?
                """,
                (request_id, InteractionStatus.PENDING.value),
            ).fetchone()
        return _interaction_from_row(row) if row is not None else None

    def attach_lark_message_id(
        self,
        interaction_id: str,
        message_id: str,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE codex_interactions SET lark_message_id = ?
                WHERE id = ? AND status = ? AND lark_message_id IS NULL
                """,
                (message_id, interaction_id, InteractionStatus.PENDING.value),
            )
        return cursor.rowcount == 1

    def resolve_interaction(
        self,
        interaction_id: str,
        *,
        decision: str,
        actor_id: str,
        resolved_at: datetime | None = None,
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT kind FROM codex_interactions WHERE id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                return False
            normalized_decision = _normalize_decision(row["kind"], decision)
            cursor = connection.execute(
                """
                UPDATE codex_interactions
                SET status = ?, decision = ?, actor_id = ?, resolved_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    InteractionStatus.RESOLVED.value,
                    normalized_decision.value,
                    actor_id,
                    _serialize_datetime(resolved_at or datetime.now(timezone.utc)),
                    interaction_id,
                    InteractionStatus.PENDING.value,
                ),
            )
        return cursor.rowcount == 1

    def resolve_interaction_and_refresh_session(
        self,
        interaction_id: str,
        *,
        decision: str,
        actor_id: str,
        updated_at: datetime,
        status: InteractionStatus = InteractionStatus.RESOLVED,
    ) -> bool:
        status = InteractionStatus(status)
        if status not in {InteractionStatus.RESOLVED, InteractionStatus.EXPIRED}:
            raise ValueError("status must be resolved or expired")
        timestamp = _serialize_datetime(updated_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, session_id FROM codex_interactions WHERE id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                return False
            normalized_decision = _normalize_decision(row["kind"], decision)
            cursor = connection.execute(
                """
                UPDATE codex_interactions
                SET status = ?, decision = ?, actor_id = ?, resolved_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status.value,
                    normalized_decision.value,
                    actor_id,
                    timestamp,
                    interaction_id,
                    InteractionStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            pending_kinds = {
                InteractionKind(pending["kind"])
                for pending in connection.execute(
                    """
                    SELECT kind FROM codex_interactions
                    WHERE session_id = ? AND status = ?
                    """,
                    (row["session_id"], InteractionStatus.PENDING.value),
                ).fetchall()
            }
            if InteractionKind.USER_INPUT in pending_kinds:
                next_status = SessionStatus.WAITING_FOR_INPUT
            elif pending_kinds:
                next_status = SessionStatus.WAITING_FOR_APPROVAL
            else:
                next_status = SessionStatus.RUNNING
            active_values = tuple(value.value for value in _ACTIVE_SESSION_STATUSES)
            placeholders = ", ".join("?" for _ in active_values)
            connection.execute(
                f"""
                UPDATE codex_sessions SET status = ?, updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (next_status.value, timestamp, row["session_id"], *active_values),
            )
        return True

    def claim_session_terminal(
        self,
        session_id: str,
        terminal_status: SessionStatus,
        summary: str,
        pending_status: InteractionStatus,
        updated_at: datetime,
    ) -> list[str] | None:
        terminal_status = SessionStatus(terminal_status)
        if terminal_status not in _TERMINAL_SESSION_STATUSES:
            raise ValueError("terminal_status must be terminal")
        pending_status = InteractionStatus(pending_status)
        if pending_status not in {
            InteractionStatus.EXPIRED,
            InteractionStatus.CANCELLED,
        }:
            raise ValueError("pending_status must be expired or cancelled")
        active_values = tuple(status.value for status in _ACTIVE_SESSION_STATUSES)
        placeholders = ", ".join("?" for _ in active_values)
        timestamp = _serialize_datetime(updated_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE codex_sessions
                SET status = ?, summary = ?, updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (
                    terminal_status.value,
                    _safe_summary(summary),
                    timestamp,
                    session_id,
                    *active_values,
                ),
            )
            if cursor.rowcount != 1:
                return None
            interaction_ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM codex_interactions
                    WHERE session_id = ? AND status = ? ORDER BY id
                    """,
                    (session_id, InteractionStatus.PENDING.value),
                ).fetchall()
            ]
            connection.execute(
                """
                UPDATE codex_interactions SET status = ?, resolved_at = ?
                WHERE session_id = ? AND status = ?
                """,
                (
                    pending_status.value,
                    timestamp,
                    session_id,
                    InteractionStatus.PENDING.value,
                ),
            )
        return interaction_ids

    def expire_interaction(
        self,
        interaction_id: str,
        *,
        resolved_at: datetime | None = None,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE codex_interactions
                SET status = ?, resolved_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    InteractionStatus.EXPIRED.value,
                    _serialize_datetime(resolved_at or datetime.now(timezone.utc)),
                    interaction_id,
                    InteractionStatus.PENDING.value,
                ),
            )
        return cursor.rowcount == 1

    def cancel_pending_interactions(
        self,
        session_id: str,
        *,
        status: InteractionStatus,
        resolved_at: datetime | None = None,
    ) -> list[str]:
        status = InteractionStatus(status)
        if status not in {InteractionStatus.EXPIRED, InteractionStatus.CANCELLED}:
            raise ValueError("status must be expired or cancelled")
        timestamp = _serialize_datetime(resolved_at or datetime.now(timezone.utc))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            interaction_ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM codex_interactions
                    WHERE session_id = ? AND status = ?
                    ORDER BY id
                    """,
                    (session_id, InteractionStatus.PENDING.value),
                ).fetchall()
            ]
            connection.execute(
                """
                UPDATE codex_interactions
                SET status = ?, resolved_at = ?
                WHERE session_id = ? AND status = ?
                """,
                (
                    status.value,
                    timestamp,
                    session_id,
                    InteractionStatus.PENDING.value,
                ),
            )
        return interaction_ids

    def record_event_once(
        self,
        event_id: str,
        *,
        received_at: datetime | None = None,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO codex_event_dedupe (event_id, received_at)
                VALUES (?, ?)
                """,
                (
                    event_id,
                    _serialize_datetime(received_at or datetime.now(timezone.utc)),
                ),
            )
        return cursor.rowcount == 1

    def enqueue_outbox(
        self,
        *,
        notification_type: str,
        payload_summary: str,
        session_id: str | None = None,
        interaction_id: str | None = None,
        next_attempt_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> int:
        created = created_at or datetime.now(timezone.utc)
        due = next_attempt_at or created
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO notification_outbox (
                    session_id, interaction_id, notification_type,
                    payload_summary, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    interaction_id,
                    notification_type,
                    _safe_summary(payload_summary),
                    _serialize_datetime(due),
                    _serialize_datetime(created),
                ),
            )
        return int(cursor.lastrowid)

    def get_outbox_item(self, outbox_id: int) -> NotificationOutboxItem | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE id = ?",
                (outbox_id,),
            ).fetchone()
        return _outbox_from_row(row) if row is not None else None

    def list_due_outbox(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[NotificationOutboxItem]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE sent_at IS NULL AND next_attempt_at <= ?
                ORDER BY next_attempt_at, id
                LIMIT ?
                """,
                (
                    _serialize_datetime(now or datetime.now(timezone.utc)),
                    limit,
                ),
            ).fetchall()
        return [_outbox_from_row(row) for row in rows]

    def mark_outbox_sent(
        self,
        outbox_id: int,
        *,
        sent_at: datetime | None = None,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE notification_outbox
                SET sent_at = ?, last_error = NULL
                WHERE id = ? AND sent_at IS NULL
                """,
                (
                    _serialize_datetime(sent_at or datetime.now(timezone.utc)),
                    outbox_id,
                ),
            )
        return cursor.rowcount == 1

    def record_outbox_failure(
        self,
        outbox_id: int,
        *,
        error: str,
        next_attempt_at: datetime,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE notification_outbox
                SET attempt_count = attempt_count + 1,
                    next_attempt_at = ?,
                    last_error = ?
                WHERE id = ? AND sent_at IS NULL
                """,
                (
                    _serialize_datetime(next_attempt_at),
                    _safe_summary(error),
                    outbox_id,
                ),
            )
        return cursor.rowcount == 1

    def reconcile_startup(
        self,
        *,
        now: datetime | None = None,
    ) -> StartupReconciliationResult:
        reconciled_at = _serialize_datetime(now or datetime.now(timezone.utc))
        active_statuses = (
            SessionStatus.STARTING.value,
            SessionStatus.RUNNING.value,
            SessionStatus.WAITING_FOR_APPROVAL.value,
            SessionStatus.WAITING_FOR_INPUT.value,
        )
        placeholders = ", ".join("?" for _ in active_statuses)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session_ids = [
                row["id"]
                for row in connection.execute(
                    f"""
                    SELECT id FROM codex_sessions
                    WHERE status IN ({placeholders})
                    ORDER BY id
                    """,
                    active_statuses,
                ).fetchall()
            ]
            interaction_ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM codex_interactions
                    WHERE status = ?
                    ORDER BY id
                    """,
                    (InteractionStatus.PENDING.value,),
                ).fetchall()
            ]
            connection.execute(
                f"""
                UPDATE codex_sessions
                SET status = ?, updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (
                    SessionStatus.INTERRUPTED.value,
                    reconciled_at,
                    *active_statuses,
                ),
            )
            connection.execute(
                """
                UPDATE codex_interactions
                SET status = ?, resolved_at = ?
                WHERE status = ?
                """,
                (
                    InteractionStatus.EXPIRED.value,
                    reconciled_at,
                    InteractionStatus.PENDING.value,
                ),
            )
        return StartupReconciliationResult(
            session_ids=session_ids,
            interaction_ids=interaction_ids,
        )

    def record_audit(
        self,
        *,
        event_type: str,
        detail_summary: str = "",
        session_id: str | None = None,
        interaction_id: str | None = None,
        actor_id: str | None = None,
        created_at: datetime | None = None,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO codex_audit (
                    session_id, interaction_id, event_type, actor_id,
                    detail_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    interaction_id,
                    event_type,
                    actor_id,
                    _safe_summary(detail_summary),
                    _serialize_datetime(created_at or datetime.now(timezone.utc)),
                ),
            )
        return int(cursor.lastrowid)

    def list_audit(
        self,
        *,
        session_id: str | None = None,
    ) -> list[CodexAuditEntry]:
        query = "SELECT * FROM codex_audit"
        parameters: tuple[str, ...] = ()
        if session_id is not None:
            query += " WHERE session_id = ?"
            parameters = (session_id,)
        query += " ORDER BY created_at, id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_audit_from_row(row) for row in rows]

    def _init_schema(self) -> None:
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported Codex schema version {version}; "
                    f"maximum is {_SCHEMA_VERSION}"
                )
            if version == _SCHEMA_VERSION:
                return

            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            for target_version in range(version + 1, _SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[target_version]:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {target_version}")
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise RuntimeError("SQLiteCodexStore is closed")
        connection = self._memory_connection
        owns_connection = connection is None
        if connection is None:
            connection = self._new_connection(str(self.path))
        try:
            with connection:
                yield connection
        finally:
            if owns_connection:
                connection.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._memory_connection is not None:
            self._memory_connection.close()
            self._memory_connection = None

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("SQLiteCodexStore is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def _new_connection(database: str) -> sqlite3.Connection:
        connection = sqlite3.connect(database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


_MIGRATIONS: dict[int, tuple[str, ...]] = {
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


def _session_from_row(row: sqlite3.Row) -> CodexSession:
    return CodexSession(
        id=row["id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        name=row["name"],
        cwd=row["cwd"],
        model=row["model"],
        sandbox=row["sandbox"],
        status=row["status"],
        summary=row["summary"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _interaction_from_row(row: sqlite3.Row) -> PendingInteraction:
    return PendingInteraction(
        id=row["id"],
        session_id=row["session_id"],
        request_id=row["request_id"],
        kind=row["kind"],
        status=row["status"],
        lark_message_id=row["lark_message_id"],
        payload_summary=row["payload_summary"],
        requested_at=datetime.fromisoformat(row["requested_at"]),
        resolved_at=(
            datetime.fromisoformat(row["resolved_at"])
            if row["resolved_at"] is not None
            else None
        ),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        actor_id=row["actor_id"],
        decision=row["decision"],
    )


def _outbox_from_row(row: sqlite3.Row) -> NotificationOutboxItem:
    return NotificationOutboxItem(
        id=row["id"],
        session_id=row["session_id"],
        interaction_id=row["interaction_id"],
        notification_type=row["notification_type"],
        payload_summary=row["payload_summary"],
        attempt_count=row["attempt_count"],
        next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]),
        sent_at=(
            datetime.fromisoformat(row["sent_at"])
            if row["sent_at"] is not None
            else None
        ),
        last_error=row["last_error"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _audit_from_row(row: sqlite3.Row) -> CodexAuditEntry:
    return CodexAuditEntry(
        id=row["id"],
        session_id=row["session_id"],
        interaction_id=row["interaction_id"],
        event_type=row["event_type"],
        actor_id=row["actor_id"],
        detail_summary=row["detail_summary"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _serialize_optional_datetime(value: datetime | None) -> str | None:
    return _serialize_datetime(value) if value is not None else None


def _safe_summary(value: str) -> str:
    return redact_text(value)[:_SUMMARY_LIMIT]


def _interaction_values(interaction: PendingInteraction) -> tuple[object, ...]:
    return (
        interaction.id,
        interaction.session_id,
        interaction.request_id,
        interaction.kind.value,
        interaction.status.value,
        interaction.lark_message_id,
        _safe_summary(interaction.payload_summary),
        _serialize_datetime(interaction.requested_at),
        _serialize_optional_datetime(interaction.resolved_at),
        _serialize_datetime(interaction.expires_at),
        interaction.actor_id,
        interaction.decision.value if interaction.decision is not None else None,
    )


def _normalize_decision(
    kind_value: str,
    decision: str,
) -> InteractionDecision:
    kind = InteractionKind(kind_value)
    if kind is InteractionKind.USER_INPUT:
        return InteractionDecision.SUBMITTED
    try:
        normalized = InteractionDecision(decision)
    except ValueError as error:
        raise ValueError(f"invalid decision for {kind.value}") from error
    if normalized not in _ALLOWED_DECISIONS[kind]:
        raise ValueError(f"invalid decision for {kind.value}")
    return normalized
