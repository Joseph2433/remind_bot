from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Iterator, Self

from lark_bot.modules.codex.codex_model import (
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
from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.codex.codex_mapper import (
    audit_from_row as _audit_from_row,
    interaction_from_row as _interaction_from_row,
    interaction_values as _interaction_values,
    outbox_from_row as _outbox_from_row,
    safe_summary as _safe_summary,
    serialize_datetime as _serialize_datetime,
    serialize_optional_datetime as _serialize_optional_datetime,
    session_from_row as _session_from_row,
)
from lark_bot.modules.codex.codex_schema import initialize_schema


_UNSET = object()
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

    def get_pending_interaction_by_lark_message_id(
        self, message_id: str
    ) -> PendingInteraction | None:
        if not message_id:
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM codex_interactions
                WHERE lark_message_id = ? AND status = ?
                """,
                (message_id, InteractionStatus.PENDING.value),
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

    def cancel_interaction_and_refresh_session(
        self,
        interaction_id: str,
        *,
        updated_at: datetime,
    ) -> bool:
        timestamp = _serialize_datetime(updated_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT session_id FROM codex_interactions WHERE id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """
                UPDATE codex_interactions
                SET status = ?, resolved_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    InteractionStatus.CANCELLED.value,
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

    def finish_interactive_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        summary: str,
        updated_at: datetime,
    ) -> list[str] | None:
        if not turn_id:
            raise ValueError("turn_id must be a non-empty string")
        active_values = tuple(status.value for status in _ACTIVE_SESSION_STATUSES)
        placeholders = ", ".join("?" for _ in active_values)
        timestamp = _serialize_datetime(updated_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE codex_sessions
                SET status = ?, turn_id = NULL, summary = ?, updated_at = ?
                WHERE id = ? AND turn_id = ? AND status IN ({placeholders})
                """,
                (
                    SessionStatus.RUNNING.value,
                    _safe_summary(summary),
                    timestamp,
                    session_id,
                    turn_id,
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
                    InteractionStatus.CANCELLED.value,
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
        agent: AgentKind | str | None = None,
        session_name: str | None = None,
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
                    session_id, agent, session_name, interaction_id, notification_type,
                    payload_summary, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent.value if isinstance(agent, AgentKind) else agent,
                    session_name,
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
            initialize_schema(connection)

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
