from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Iterator, Self

from lark_bot.modules.agent.agent_model import (
    AgentInteraction, AgentKind, AgentSession, InteractionKind,
    InteractionStatus as SharedInteractionStatus, SessionStatus as SharedSessionStatus,
)
from lark_bot.modules.agent.agent_store import SQLiteAgentStore
from lark_bot.modules.codex.codex_model import (
    CodexAuditEntry, CodexSession, InteractionDecision, InteractionStatus,
    NotificationOutboxItem, PendingInteraction, SessionStatus, StartupReconciliationResult,
)

_UNSET = object()


def _session_to_agent(session: CodexSession) -> AgentSession:
    return AgentSession(session_id=session.id, agent=AgentKind.CODEX, name=session.name, conversation_id=session.thread_id, turn_id=session.turn_id, cwd=session.cwd, model=session.model, sandbox=session.sandbox, status=SharedSessionStatus(session.status.value), summary=session.summary, created_at=session.created_at, updated_at=session.updated_at)


def _session_from_agent(session: AgentSession) -> CodexSession:
    return CodexSession(id=session.session_id, thread_id=session.conversation_id, turn_id=session.turn_id, name=session.name, cwd=session.cwd, model=session.model, sandbox=session.sandbox, status=SessionStatus(session.status.value), summary=session.summary, created_at=session.created_at, updated_at=session.updated_at)


def _interaction_to_agent(interaction: PendingInteraction) -> AgentInteraction:
    return AgentInteraction(interaction_id=interaction.id, session_id=interaction.session_id, request_id=interaction.request_id, kind=interaction.kind, status=SharedInteractionStatus(interaction.status.value), lark_message_id=interaction.lark_message_id, payload_summary=interaction.payload_summary, requested_at=interaction.requested_at, resolved_at=interaction.resolved_at, expires_at=interaction.expires_at, actor_id=interaction.actor_id, decision=interaction.decision.value if interaction.decision else None)


def _interaction_from_agent(interaction: AgentInteraction) -> PendingInteraction:
    return PendingInteraction(id=interaction.interaction_id, session_id=interaction.session_id, request_id=interaction.request_id, kind=InteractionKind(interaction.kind), status=InteractionStatus(interaction.status.value), lark_message_id=interaction.lark_message_id, payload_summary=interaction.payload_summary, requested_at=interaction.requested_at, resolved_at=interaction.resolved_at, expires_at=interaction.expires_at or interaction.requested_at, actor_id=interaction.actor_id, decision=InteractionDecision(interaction.decision) if interaction.decision else None)


class SQLiteCodexStore:
    def __init__(self, path: str | Path) -> None:
        self._store = SQLiteAgentStore(path)
        self.database = self._store.database

    def create_session(self, session: CodexSession) -> None: self._store.create(_session_to_agent(session))
    def get_session(self, session_id: str) -> CodexSession | None:
        value = self._store.get(session_id, agent=AgentKind.CODEX); return _session_from_agent(value) if value else None
    def get_session_by_thread(self, thread_id: str) -> CodexSession | None:
        value = self._store.get_by_conversation(thread_id, agent=AgentKind.CODEX); return _session_from_agent(value) if value else None
    def list_sessions(self, status: SessionStatus | None = None) -> list[CodexSession]:
        return [_session_from_agent(s) for s in self._store.list(status=SharedSessionStatus(status.value) if status else None, agent=AgentKind.CODEX)]

    def update_session(self, session_id: str, *, status: SessionStatus | None = None, thread_id: str | None | object = _UNSET, turn_id: str | None | object = _UNSET, summary: str | None = None, updated_at: datetime | None = None) -> CodexSession | None:
        current = self._store.get(session_id, agent=AgentKind.CODEX)
        if current is None: return None
        data = current.model_copy(update={"status": SharedSessionStatus(status.value) if status else current.status, "conversation_id": thread_id if thread_id is not _UNSET else current.conversation_id, "turn_id": turn_id if turn_id is not _UNSET else current.turn_id, "summary": summary if summary is not None else current.summary, "updated_at": updated_at or datetime.now(timezone.utc)})
        self._store.update(data); return _session_from_agent(data)

    def update_session_if_status(self, session_id: str, expected_statuses: tuple[SessionStatus, ...], *, status: SessionStatus, thread_id: str | None | object = _UNSET, turn_id: str | None | object = _UNSET, summary: str | None = None, updated_at: datetime | None = None) -> bool:
        return self._store.update_if_status(session_id, tuple(SharedSessionStatus(v.value) for v in expected_statuses), status=SharedSessionStatus(status.value), updated_at=updated_at, summary=summary, conversation_id=thread_id if thread_id is not _UNSET else None, turn_id=turn_id if turn_id is not _UNSET else None)

    def create_interaction(self, interaction: PendingInteraction) -> None: self._store.create_interaction(_interaction_to_agent(interaction))
    def create_interaction_and_mark_waiting(self, interaction: PendingInteraction, waiting_status: SessionStatus, updated_at: datetime) -> bool: return self._store.create_interaction_and_mark_waiting(_interaction_to_agent(interaction), SharedSessionStatus(waiting_status.value), updated_at)
    def get_interaction(self, interaction_id: str) -> PendingInteraction | None:
        value = self._store.get_interaction(interaction_id, agent=AgentKind.CODEX); return _interaction_from_agent(value) if value else None
    def get_pending_interaction(self, request_id: str) -> PendingInteraction | None:
        value = self._store.get_pending_interaction(request_id, agent=AgentKind.CODEX); return _interaction_from_agent(value) if value else None
    def get_pending_interaction_by_lark_message_id(self, message_id: str) -> PendingInteraction | None:
        value = self._store.get_pending_interaction_by_lark_message_id(message_id, agent=AgentKind.CODEX); return _interaction_from_agent(value) if value else None
    def attach_lark_message_id(self, interaction_id: str, message_id: str) -> bool: return self._store.attach_lark_message_id(interaction_id, message_id)
    def resolve_interaction(self, interaction_id: str, *, decision: str, actor_id: str, resolved_at: datetime | None = None) -> bool: return self._store.resolve_interaction(interaction_id, decision=decision, actor_id=actor_id, resolved_at=resolved_at)
    def resolve_interaction_and_refresh_session(self, interaction_id: str, *, decision: str, actor_id: str, updated_at: datetime, status: InteractionStatus = InteractionStatus.RESOLVED) -> bool: return self._store.resolve_interaction_and_refresh_session(interaction_id, decision=decision, actor_id=actor_id, updated_at=updated_at, status=SharedInteractionStatus(status.value))
    def cancel_interaction_and_refresh_session(self, interaction_id: str, *, updated_at: datetime) -> bool: return self._store.cancel_interaction_and_refresh_session(interaction_id, updated_at=updated_at)
    def expire_interaction(self, interaction_id: str, *, resolved_at: datetime | None = None) -> bool: return self._store.expire_interaction(interaction_id, resolved_at=resolved_at)
    def cancel_pending_interactions(self, session_id: str, *, status: InteractionStatus, resolved_at: datetime | None = None) -> list[str]: return self._store.cancel_pending_interactions(session_id, status=SharedInteractionStatus(status.value), resolved_at=resolved_at)
    def claim_session_terminal(self, session_id: str, terminal_status: SessionStatus, summary: str, pending_status: InteractionStatus, updated_at: datetime) -> list[str] | None: return self._store.claim_session_terminal(session_id, SharedSessionStatus(terminal_status.value), summary, SharedInteractionStatus(pending_status.value), updated_at)
    def finish_interactive_turn(self, session_id: str, *, turn_id: str, summary: str, updated_at: datetime) -> list[str] | None: return self._store.finish_interactive_turn(session_id, turn_id=turn_id, summary=summary, updated_at=updated_at)
    def record_event_once(self, event_id: str, *, received_at: datetime | None = None) -> bool: return self._store.record_event_once(event_id, received_at=received_at)
    def enqueue_outbox(self, **kwargs: object) -> int:
        kwargs.setdefault("agent", AgentKind.CODEX)
        return self._store.enqueue_outbox(**kwargs)  # type: ignore[arg-type]
    def get_outbox_item(self, outbox_id: int) -> NotificationOutboxItem | None:
        value = self._store.get_outbox_item(outbox_id)
        return NotificationOutboxItem.model_validate(value.model_dump()) if value else None
    def list_due_outbox(self, *, now: datetime | None = None, limit: int = 100) -> list[NotificationOutboxItem]:
        return [NotificationOutboxItem.model_validate(v.model_dump()) for v in self._store.list_due_outbox(now=now, limit=limit, agent=AgentKind.CODEX)]
    def mark_outbox_sent(self, outbox_id: int, *, sent_at: datetime | None = None) -> bool: return self._store.mark_outbox_sent(outbox_id, sent_at=sent_at)
    def record_outbox_failure(self, outbox_id: int, *, error: str, next_attempt_at: datetime) -> bool: return self._store.record_outbox_failure(outbox_id, error=error, next_attempt_at=next_attempt_at)
    def reconcile_startup(self, *, now: datetime | None = None) -> StartupReconciliationResult: return StartupReconciliationResult.model_validate(self._store.reconcile_startup(now=now, agent=AgentKind.CODEX).model_dump())
    def record_audit(self, **kwargs: object) -> int: return self._store.record_audit(**kwargs)  # type: ignore[arg-type]
    def list_audit(self, *, session_id: str | None = None) -> list[CodexAuditEntry]: return [CodexAuditEntry.model_validate(v.model_dump()) for v in self._store.list_audit(session_id=session_id, agent=AgentKind.CODEX)]

    @contextmanager
    def _connection(self) -> Iterator[object]:
        with self._store._connection() as connection: yield connection
    def close(self) -> None: self._store.close()
    def __enter__(self) -> Self:
        self._store.__enter__(); return self
    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None: self._store.__exit__(exc_type, exc_value, traceback)
