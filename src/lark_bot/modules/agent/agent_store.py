from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from types import TracebackType
from typing import Iterator, Protocol, Self

from lark_bot.modules.agent.agent_model import (
    AgentAuditEntry, AgentInteraction, AgentKind, AgentNotification, AgentSession,
    InteractionKind, InteractionStatus, SessionStatus, StartupReconciliationResult,
)
from lark_bot.modules.agent.agent_mapper import (
    interaction_from_row, interaction_values, safe_summary, serialize_datetime,
    session_from_row, session_values,
)
from lark_bot.modules.agent.agent_schema import initialize_schema


class AgentSessionStore(Protocol):
    def create(self, session: AgentSession) -> None:
        """Persist a new session."""

    def get(self, session_id: str) -> AgentSession | None:
        """Return one session by its stable ID."""

    def list(self) -> Iterable[AgentSession]:
        """Return all sessions in deterministic order."""

    def update(self, session: AgentSession) -> None:
        """Persist the current session state."""


_ACTIVE = tuple(SessionStatus(value) for value in ("starting", "running", "waiting", "waiting_for_approval", "waiting_for_input"))
_TERMINAL = tuple(SessionStatus(value) for value in ("succeeded", "failed", "interrupted", "cancelled"))


class SQLiteAgentStore:
    def __init__(self, path: str | Path) -> None:
        self.database = str(path)
        self._closed = False
        self._memory_connection: sqlite3.Connection | None = None
        if self.database == ":memory:":
            self._memory_connection = self._new_connection(self.database)
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            initialize_schema(connection)

    def create(self, session: AgentSession) -> None:
        with self._connection() as c:
            c.execute("""INSERT INTO agent_sessions
                (session_id,agent,name,conversation_id,turn_id,cwd,model,sandbox,permission_mode,status,summary,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", session_values(session))

    def get(self, session_id: str, *, agent: AgentKind | str | None = None) -> AgentSession | None:
        query = "SELECT * FROM agent_sessions WHERE session_id = ?"
        args: list[object] = [session_id]
        if agent is not None:
            query += " AND agent = ?"; args.append(AgentKind(agent).value)
        with self._connection() as c:
            row = c.execute(query, args).fetchone()
        return session_from_row(row) if row else None

    def get_by_conversation(self, conversation_id: str, *, agent: AgentKind | str | None = None) -> AgentSession | None:
        query = "SELECT * FROM agent_sessions WHERE conversation_id = ?"; args: list[object] = [conversation_id]
        if agent is not None:
            query += " AND agent = ?"; args.append(AgentKind(agent).value)
        query += " ORDER BY created_at, session_id LIMIT 1"
        with self._connection() as c: row = c.execute(query, args).fetchone()
        return session_from_row(row) if row else None

    def list(self, *, status: SessionStatus | None = None, agent: AgentKind | str | None = None) -> list[AgentSession]:
        query = "SELECT * FROM agent_sessions"; clauses: list[str] = []; args: list[object] = []
        if status is not None: clauses.append("status = ?"); args.append(SessionStatus(status).value)
        if agent is not None: clauses.append("agent = ?"); args.append(AgentKind(agent).value)
        if clauses: query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, session_id"
        with self._connection() as c: rows = c.execute(query, args).fetchall()
        return [session_from_row(row) for row in rows]

    def update(self, session: AgentSession) -> None:
        with self._connection() as c:
            c.execute("""UPDATE agent_sessions SET agent=?,name=?,conversation_id=?,turn_id=?,cwd=?,model=?,sandbox=?,permission_mode=?,status=?,summary=?,created_at=?,updated_at=? WHERE session_id=?""", session_values(session)[1:] + (session.session_id,))

    def update_if_status(self, session_id: str, expected_statuses: tuple[SessionStatus, ...], *, status: SessionStatus, updated_at: datetime | None = None, summary: str | None = None, conversation_id: str | None | object = None, turn_id: str | None | object = None) -> bool:
        if not expected_statuses: return False
        assignments = ["status = ?", "updated_at = ?"]; args: list[object] = [SessionStatus(status).value, serialize_datetime(updated_at or datetime.now(timezone.utc))]
        if summary is not None: assignments.append("summary = ?"); args.append(safe_summary(summary))
        if conversation_id is not None: assignments.append("conversation_id = ?"); args.append(conversation_id)
        if turn_id is not None: assignments.append("turn_id = ?"); args.append(turn_id)
        placeholders = ",".join("?" for _ in expected_statuses); args.extend([session_id, *(SessionStatus(s).value for s in expected_statuses)])
        with self._connection() as c:
            cur = c.execute(f"UPDATE agent_sessions SET {', '.join(assignments)} WHERE session_id = ? AND status IN ({placeholders})", args)
        return cur.rowcount == 1

    def create_interaction(self, interaction: AgentInteraction) -> None:
        if interaction.status is InteractionStatus.PENDING and any((interaction.resolved_at, interaction.actor_id, interaction.decision)):
            raise ValueError("pending interaction cannot contain resolution metadata")
        with self._connection() as c:
            c.execute("""INSERT INTO agent_interactions
                (interaction_id,session_id,request_id,kind,status,lark_message_id,payload_summary,requested_at,resolved_at,expires_at,actor_id,decision)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", interaction_values(interaction))

    def get_interaction(self, interaction_id: str, *, agent: AgentKind | str | None = None) -> AgentInteraction | None:
        query = "SELECT i.* FROM agent_interactions i"; args: list[object] = [interaction_id]
        if agent is not None: query += " JOIN agent_sessions s ON s.session_id=i.session_id AND s.agent=?"; args.insert(0, AgentKind(agent).value)
        query += " WHERE i.interaction_id = ?"
        with self._connection() as c: row = c.execute(query, args).fetchone()
        return interaction_from_row(row) if row else None

    def get_pending_interaction(self, request_id: str, *, agent: AgentKind | str | None = None) -> AgentInteraction | None:
        query = "SELECT i.* FROM agent_interactions i"; args: list[object] = []
        if agent is not None: query += " JOIN agent_sessions s ON s.session_id=i.session_id AND s.agent=?"; args.append(AgentKind(agent).value)
        query += " WHERE i.request_id = ? AND i.status = ?"; args.extend([request_id, InteractionStatus.PENDING.value])
        with self._connection() as c: row = c.execute(query, args).fetchone()
        return interaction_from_row(row) if row else None

    def get_pending_interaction_by_lark_message_id(self, message_id: str, *, agent: AgentKind | str | None = None) -> AgentInteraction | None:
        if not message_id: return None
        query = "SELECT i.* FROM agent_interactions i"; args: list[object] = []
        if agent is not None: query += " JOIN agent_sessions s ON s.session_id=i.session_id AND s.agent=?"; args.append(AgentKind(agent).value)
        query += " WHERE i.lark_message_id = ? AND i.status = ?"; args.extend([message_id, InteractionStatus.PENDING.value])
        with self._connection() as c: row = c.execute(query, args).fetchone()
        return interaction_from_row(row) if row else None

    def create_interaction_and_mark_waiting(self, interaction: AgentInteraction, waiting_status: SessionStatus, updated_at: datetime) -> bool:
        if SessionStatus(waiting_status) not in {SessionStatus.WAITING_FOR_APPROVAL, SessionStatus.WAITING_FOR_INPUT}: raise ValueError("waiting_status must be a waiting session status")
        try:
            with self._connection() as c:
                c.execute("BEGIN IMMEDIATE")
                cur = c.execute("UPDATE agent_sessions SET status=?,updated_at=? WHERE session_id=? AND status IN (?,?,?,?,?)", (SessionStatus(waiting_status).value,serialize_datetime(updated_at),interaction.session_id,*[v.value for v in _ACTIVE]))
                if cur.rowcount != 1: return False
                c.execute("""INSERT INTO agent_interactions (interaction_id,session_id,request_id,kind,status,lark_message_id,payload_summary,requested_at,resolved_at,expires_at,actor_id,decision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", interaction_values(interaction))
        except sqlite3.IntegrityError: return False
        return True

    def attach_lark_message_id(self, interaction_id: str, message_id: str) -> bool:
        with self._connection() as c: cur = c.execute("UPDATE agent_interactions SET lark_message_id=? WHERE interaction_id=? AND status=? AND lark_message_id IS NULL", (message_id,interaction_id,InteractionStatus.PENDING.value))
        return cur.rowcount == 1

    def resolve_interaction(self, interaction_id: str, *, decision: str, actor_id: str, resolved_at: datetime | None = None) -> bool:
        with self._connection() as c:
            row = c.execute("SELECT kind FROM agent_interactions WHERE interaction_id=?", (interaction_id,)).fetchone()
            if not row: return False
            normalized = self._normalize_decision(row["kind"], decision)
            cur = c.execute("UPDATE agent_interactions SET status=?,decision=?,actor_id=?,resolved_at=? WHERE interaction_id=? AND status=?", (InteractionStatus.RESOLVED.value,normalized,actor_id,serialize_datetime(resolved_at or datetime.now(timezone.utc)),interaction_id,InteractionStatus.PENDING.value))
        return cur.rowcount == 1

    def resolve_interaction_and_refresh_session(self, interaction_id: str, *, decision: str, actor_id: str, updated_at: datetime, status: InteractionStatus = InteractionStatus.RESOLVED) -> bool:
        status = InteractionStatus(status)
        if status not in {InteractionStatus.RESOLVED, InteractionStatus.EXPIRED}: raise ValueError("status must be resolved or expired")
        stamp = serialize_datetime(updated_at)
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE"); row = c.execute("SELECT kind,session_id FROM agent_interactions WHERE interaction_id=?", (interaction_id,)).fetchone()
            if not row: return False
            normalized = self._normalize_decision(row["kind"], decision)
            cur = c.execute("UPDATE agent_interactions SET status=?,decision=?,actor_id=?,resolved_at=? WHERE interaction_id=? AND status=?", (status.value,normalized,actor_id,stamp,interaction_id,InteractionStatus.PENDING.value))
            if cur.rowcount != 1: return False
            pending = c.execute("SELECT kind FROM agent_interactions WHERE session_id=? AND status=?", (row["session_id"],InteractionStatus.PENDING.value)).fetchall()
            next_status = SessionStatus.WAITING_FOR_INPUT if any(p["kind"] == InteractionKind.USER_INPUT.value for p in pending) else (SessionStatus.WAITING_FOR_APPROVAL if pending else SessionStatus.RUNNING)
            c.execute("UPDATE agent_sessions SET status=?,updated_at=? WHERE session_id=? AND status IN (?,?,?,?,?)", (next_status.value,stamp,row["session_id"],*[v.value for v in _ACTIVE]))
        return True

    def cancel_interaction_and_refresh_session(self, interaction_id: str, *, updated_at: datetime) -> bool:
        stamp = serialize_datetime(updated_at)
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT session_id FROM agent_interactions WHERE interaction_id=?", (interaction_id,)).fetchone()
            if not row:
                return False
            cur = c.execute("UPDATE agent_interactions SET status=?,resolved_at=? WHERE interaction_id=? AND status=?", (InteractionStatus.CANCELLED.value, stamp, interaction_id, InteractionStatus.PENDING.value))
            if cur.rowcount != 1:
                return False
            pending = c.execute("SELECT kind FROM agent_interactions WHERE session_id=? AND status=?", (row["session_id"], InteractionStatus.PENDING.value)).fetchall()
            next_status = SessionStatus.WAITING_FOR_INPUT if any(p["kind"] == InteractionKind.USER_INPUT.value for p in pending) else (SessionStatus.WAITING_FOR_APPROVAL if pending else SessionStatus.RUNNING)
            c.execute("UPDATE agent_sessions SET status=?,updated_at=? WHERE session_id=? AND status IN (?,?,?,?,?)", (next_status.value, stamp, row["session_id"], *[v.value for v in _ACTIVE]))
        return True

    def expire_interaction(self, interaction_id: str, *, resolved_at: datetime | None = None) -> bool:
        with self._connection() as c: cur = c.execute("UPDATE agent_interactions SET status=?,resolved_at=? WHERE interaction_id=? AND status=?", (InteractionStatus.EXPIRED.value,serialize_datetime(resolved_at or datetime.now(timezone.utc)),interaction_id,InteractionStatus.PENDING.value))
        return cur.rowcount == 1

    def cancel_pending_interactions(self, session_id: str, *, status: InteractionStatus, resolved_at: datetime | None = None) -> list[str]:
        status = InteractionStatus(status)
        if status not in {InteractionStatus.EXPIRED, InteractionStatus.CANCELLED}: raise ValueError("status must be expired or cancelled")
        stamp = serialize_datetime(resolved_at or datetime.now(timezone.utc))
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE"); ids = [row["interaction_id"] for row in c.execute("SELECT interaction_id FROM agent_interactions WHERE session_id=? AND status=? ORDER BY interaction_id", (session_id,InteractionStatus.PENDING.value)).fetchall()]; c.execute("UPDATE agent_interactions SET status=?,resolved_at=? WHERE session_id=? AND status=?", (status.value,stamp,session_id,InteractionStatus.PENDING.value))
        return ids

    def claim_session_terminal(self, session_id: str, terminal_status: SessionStatus, summary: str, pending_status: InteractionStatus, updated_at: datetime) -> list[str] | None:
        if SessionStatus(terminal_status) not in _TERMINAL: raise ValueError("terminal_status must be terminal")
        if InteractionStatus(pending_status) not in {InteractionStatus.EXPIRED, InteractionStatus.CANCELLED}: raise ValueError("pending_status must be expired or cancelled")
        stamp = serialize_datetime(updated_at)
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE"); cur = c.execute("UPDATE agent_sessions SET status=?,summary=?,updated_at=? WHERE session_id=? AND status IN (?,?,?,?,?)", (SessionStatus(terminal_status).value,safe_summary(summary),stamp,session_id,*[v.value for v in _ACTIVE]))
            if cur.rowcount != 1: return None
            ids = [r["interaction_id"] for r in c.execute("SELECT interaction_id FROM agent_interactions WHERE session_id=? AND status=? ORDER BY interaction_id", (session_id,InteractionStatus.PENDING.value)).fetchall()]; c.execute("UPDATE agent_interactions SET status=?,resolved_at=? WHERE session_id=? AND status=?", (InteractionStatus(pending_status).value,stamp,session_id,InteractionStatus.PENDING.value))
        return ids

    def finish_interactive_turn(self, session_id: str, *, turn_id: str, summary: str, updated_at: datetime) -> list[str] | None:
        if not turn_id: raise ValueError("turn_id must be a non-empty string")
        stamp = serialize_datetime(updated_at)
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE"); cur = c.execute("UPDATE agent_sessions SET status=?,turn_id=NULL,summary=?,updated_at=? WHERE session_id=? AND turn_id=? AND status IN (?,?,?,?,?)", (SessionStatus.RUNNING.value,safe_summary(summary),stamp,session_id,turn_id,*[v.value for v in _ACTIVE]))
            if cur.rowcount != 1: return None
            ids = [r["interaction_id"] for r in c.execute("SELECT interaction_id FROM agent_interactions WHERE session_id=? AND status=? ORDER BY interaction_id", (session_id,InteractionStatus.PENDING.value)).fetchall()]; c.execute("UPDATE agent_interactions SET status=?,resolved_at=? WHERE session_id=? AND status=?", (InteractionStatus.CANCELLED.value,stamp,session_id,InteractionStatus.PENDING.value))
        return ids

    def record_event_once(self, event_id: str, *, received_at: datetime | None = None) -> bool:
        with self._connection() as c: cur = c.execute("INSERT OR IGNORE INTO agent_event_dedupe(event_id,received_at) VALUES (?,?)", (event_id,serialize_datetime(received_at or datetime.now(timezone.utc))))
        return cur.rowcount == 1

    def enqueue_outbox(self, *, notification_type: str, payload_summary: str, session_id: str | None = None, agent: AgentKind | str | None = None, session_name: str | None = None, interaction_id: str | None = None, next_attempt_at: datetime | None = None, created_at: datetime | None = None) -> int:
        created = created_at or datetime.now(timezone.utc); due = next_attempt_at or created; kind = AgentKind(agent).value if agent is not None else None
        with self._connection() as c:
            if session_name is None and session_id:
                row = c.execute("SELECT name FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone(); session_name = row[0] if row else None
            cur = c.execute("INSERT INTO agent_notification_outbox(session_id,agent,session_name,interaction_id,notification_type,payload_summary,next_attempt_at,created_at) VALUES (?,?,?,?,?,?,?,?)", (session_id,kind,session_name,interaction_id,notification_type,safe_summary(payload_summary),serialize_datetime(due),serialize_datetime(created)))
        return int(cur.lastrowid)

    def get_outbox_item(self, outbox_id: int) -> AgentNotification | None:
        with self._connection() as c: row = c.execute("SELECT * FROM agent_notification_outbox WHERE id=?", (outbox_id,)).fetchone()
        return self._notification_from_row(row) if row else None

    def list_due_outbox(self, *, now: datetime | None = None, limit: int = 100, agent: AgentKind | str | None = None) -> list[AgentNotification]:
        query = "SELECT * FROM agent_notification_outbox WHERE sent_at IS NULL AND next_attempt_at <= ?"; args: list[object] = [serialize_datetime(now or datetime.now(timezone.utc))]
        if agent is not None: query += " AND agent=?"; args.append(AgentKind(agent).value)
        query += " ORDER BY next_attempt_at,id LIMIT ?"; args.append(limit)
        with self._connection() as c: rows = c.execute(query,args).fetchall()
        return [self._notification_from_row(row) for row in rows]

    def mark_outbox_sent(self, outbox_id: int, *, sent_at: datetime | None = None) -> bool:
        with self._connection() as c: cur = c.execute("UPDATE agent_notification_outbox SET sent_at=?,last_error=NULL WHERE id=? AND sent_at IS NULL", (serialize_datetime(sent_at or datetime.now(timezone.utc)),outbox_id))
        return cur.rowcount == 1

    def record_outbox_failure(self, outbox_id: int, *, error: str, next_attempt_at: datetime) -> bool:
        with self._connection() as c: cur = c.execute("UPDATE agent_notification_outbox SET attempt_count=attempt_count+1,next_attempt_at=?,last_error=? WHERE id=? AND sent_at IS NULL", (serialize_datetime(next_attempt_at),safe_summary(error),outbox_id))
        return cur.rowcount == 1

    def reconcile_startup(self, *, now: datetime | None = None, agent: AgentKind | str | None = None) -> StartupReconciliationResult:
        stamp = serialize_datetime(now or datetime.now(timezone.utc)); args: list[object] = [*[v.value for v in _ACTIVE]]; where = "status IN (?,?,?,?,?)"
        if agent is not None: where += " AND agent=?"; args.append(AgentKind(agent).value)
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE"); sessions = [r["session_id"] for r in c.execute(f"SELECT session_id FROM agent_sessions WHERE {where} ORDER BY session_id", args).fetchall()]
            ia_args: list[object] = [InteractionStatus.PENDING.value]; ia_where = "i.status=?"
            if agent is not None: ia_where += " AND s.agent=?"; ia_args.append(AgentKind(agent).value)
            interactions = [r["interaction_id"] for r in c.execute(f"SELECT i.interaction_id FROM agent_interactions i JOIN agent_sessions s ON s.session_id=i.session_id WHERE {ia_where} ORDER BY i.interaction_id", ia_args).fetchall()]
            c.execute(f"UPDATE agent_sessions SET status=?,updated_at=? WHERE {where}", [SessionStatus.INTERRUPTED.value,stamp,*args]); c.execute(f"UPDATE agent_interactions SET status=?,resolved_at=? WHERE interaction_id IN ({','.join('?' for _ in interactions)})", [InteractionStatus.EXPIRED.value,stamp,*interactions]) if interactions else None
        return StartupReconciliationResult(session_ids=sessions,interaction_ids=interactions)

    def record_audit(self, *, event_type: str, detail_summary: str = "", session_id: str | None = None, interaction_id: str | None = None, actor_id: str | None = None, created_at: datetime | None = None) -> int:
        with self._connection() as c: cur = c.execute("INSERT INTO agent_audit(session_id,interaction_id,event_type,actor_id,detail_summary,created_at) VALUES (?,?,?,?,?,?)", (session_id,interaction_id,event_type,actor_id,safe_summary(detail_summary),serialize_datetime(created_at or datetime.now(timezone.utc))))
        return int(cur.lastrowid)

    def list_audit(self, *, session_id: str | None = None, agent: AgentKind | str | None = None) -> list[AgentAuditEntry]:
        query = "SELECT a.* FROM agent_audit a LEFT JOIN agent_sessions s ON s.session_id=a.session_id"; clauses: list[str] = []; args: list[object] = []
        if session_id is not None: clauses.append("a.session_id=?"); args.append(session_id)
        if agent is not None: clauses.append("(s.agent=? OR a.session_id IS NULL)"); args.append(AgentKind(agent).value)
        if clauses: query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY a.created_at,a.id"
        with self._connection() as c: rows = c.execute(query,args).fetchall()
        return [AgentAuditEntry(id=r["id"],session_id=r["session_id"],interaction_id=r["interaction_id"],event_type=r["event_type"],actor_id=r["actor_id"],detail_summary=r["detail_summary"],created_at=datetime.fromisoformat(r["created_at"])) for r in rows]

    @staticmethod
    def _normalize_decision(kind: str, decision: str) -> str:
        if kind == InteractionKind.USER_INPUT.value: return "submitted"
        allowed = {InteractionKind.EXEC_APPROVAL.value:{"approved","denied"},InteractionKind.FILE_CHANGE_APPROVAL.value:{"accept","decline"},InteractionKind.PERMISSION_REQUEST.value:{"granted","denied"}}
        if decision not in allowed.get(kind,set()): raise ValueError(f"invalid decision for {kind}")
        return decision

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> AgentNotification:
        return AgentNotification(id=row["id"],session_id=row["session_id"],agent=row["agent"],session_name=row["session_name"],interaction_id=row["interaction_id"],notification_type=row["notification_type"],payload_summary=row["payload_summary"],attempt_count=row["attempt_count"],next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]),sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,last_error=row["last_error"],created_at=datetime.fromisoformat(row["created_at"]))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed: raise RuntimeError("SQLiteAgentStore is closed")
        connection = self._memory_connection; owns = connection is None
        if connection is None: connection = self._new_connection(str(self.path))
        try:
            with connection: yield connection
        finally:
            if owns: connection.close()

    @staticmethod
    def _new_connection(database: str) -> sqlite3.Connection:
        c = sqlite3.connect(database, timeout=5.0); c.row_factory = sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=5000"); return c

    def close(self) -> None:
        if self._closed: return
        self._closed = True
        if self._memory_connection is not None: self._memory_connection.close(); self._memory_connection = None

    def __enter__(self) -> Self:
        if self._closed: raise RuntimeError("SQLiteAgentStore is closed")
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()
