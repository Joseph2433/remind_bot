from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Iterator, Self

from lark_bot.modules.agent.agent_model import (
    AgentInteraction,
    AgentKind,
    AgentSession,
    InteractionKind,
    InteractionStatus as SharedInteractionStatus,
    SessionStatus as SharedSessionStatus,
)
from lark_bot.modules.agent.agent_store import SQLiteAgentStore, _UNSET as SHARED_UNSET
from lark_bot.modules.codex.codex_model import (
    CodexAuditEntry,
    CodexSession,
    InteractionDecision,
    InteractionStatus,
    NotificationOutboxItem,
    PendingInteraction,
    SessionStatus,
    StartupReconciliationResult,
)

_UNSET = object()


def _session_to_agent(session: CodexSession) -> AgentSession:
    return AgentSession(
        session_id=session.id,
        agent=AgentKind.CODEX,
        name=session.name,
        conversation_id=session.thread_id,
        turn_id=session.turn_id,
        cwd=session.cwd,
        model=session.model,
        sandbox=session.sandbox,
        status=SharedSessionStatus(session.status.value),
        summary=session.summary,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _session_from_agent(session: AgentSession) -> CodexSession:
    return CodexSession(
        id=session.session_id,
        thread_id=session.conversation_id,
        turn_id=session.turn_id,
        name=session.name,
        cwd=session.cwd,
        model=session.model,
        sandbox=session.sandbox,
        status=SessionStatus(session.status.value),
        summary=session.summary,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _interaction_to_agent(interaction: PendingInteraction) -> AgentInteraction:
    return AgentInteraction(
        interaction_id=interaction.id,
        session_id=interaction.session_id,
        request_id=interaction.request_id,
        kind=interaction.kind,
        status=SharedInteractionStatus(interaction.status.value),
        lark_message_id=interaction.lark_message_id,
        payload_summary=interaction.payload_summary,
        requested_at=interaction.requested_at,
        resolved_at=interaction.resolved_at,
        expires_at=interaction.expires_at,
        actor_id=interaction.actor_id,
        decision=interaction.decision,
    )


def _interaction_from_agent(interaction: AgentInteraction) -> PendingInteraction:
    return PendingInteraction(
        id=interaction.interaction_id,
        session_id=interaction.session_id,
        request_id=interaction.request_id,
        kind=InteractionKind(interaction.kind),
        status=InteractionStatus(interaction.status.value),
        lark_message_id=interaction.lark_message_id,
        payload_summary=interaction.payload_summary,
        requested_at=interaction.requested_at,
        resolved_at=interaction.resolved_at,
        expires_at=interaction.expires_at,
        actor_id=interaction.actor_id,
        decision=InteractionDecision(interaction.decision) if interaction.decision else None,
    )


class SQLiteCodexStore:
    def __init__(self, path: str | Path) -> None:
        self._store = SQLiteAgentStore(path)
        self.database = self._store.database

    def create_session(self, session: CodexSession) -> None:
        self._store.create_session(_session_to_agent(session))

    def get_session(self, session_id: str) -> CodexSession | None:
        value = self._store.get_session(session_id, agent=AgentKind.CODEX)
        return _session_from_agent(value) if value else None

    def get_session_by_thread(self, thread_id: str) -> CodexSession | None:
        value = self._store.get_session_by_conversation(thread_id, agent=AgentKind.CODEX)
        return _session_from_agent(value) if value else None

    def list_sessions(self, status: SessionStatus | None = None) -> list[CodexSession]:
        shared_status = SharedSessionStatus(status.value) if status else None
        return [_session_from_agent(value) for value in self._store.list_sessions(shared_status, agent=AgentKind.CODEX)]

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
        changes: dict[str, object] = {}
        if status is not None:
            changes["status"] = SharedSessionStatus(status.value)
        if thread_id is not _UNSET:
            changes["conversation_id"] = thread_id
        if turn_id is not _UNSET:
            changes["turn_id"] = turn_id
        if summary is not None:
            changes["summary"] = summary
        if updated_at is not None:
            changes["updated_at"] = updated_at
        value = self._store.update_session(session_id, agent=AgentKind.CODEX, **changes)
        return _session_from_agent(value) if value else None

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
        return self._store.update_session_if_status(
            session_id,
            tuple(SharedSessionStatus(value.value) for value in expected_statuses),
            status=SharedSessionStatus(status.value),
            updated_at=updated_at,
            summary=summary if summary is not None else SHARED_UNSET,
            conversation_id=thread_id if thread_id is not _UNSET else SHARED_UNSET,
            turn_id=turn_id if turn_id is not _UNSET else SHARED_UNSET,
            agent=AgentKind.CODEX,
        )

    def create_interaction(self, interaction: PendingInteraction) -> None:
        self._store.create_interaction(_interaction_to_agent(interaction), agent=AgentKind.CODEX)

    def create_interaction_and_mark_waiting(
        self,
        interaction: PendingInteraction,
        waiting_status: SessionStatus,
        updated_at: datetime,
    ) -> bool:
        return self._store.create_interaction_and_mark_waiting(
            _interaction_to_agent(interaction),
            SharedSessionStatus(waiting_status.value),
            updated_at,
            agent=AgentKind.CODEX,
        )

    def get_interaction(self, interaction_id: str) -> PendingInteraction | None:
        value = self._store.get_interaction(interaction_id, agent=AgentKind.CODEX)
        return _interaction_from_agent(value) if value else None

    def get_pending_interaction(self, request_id: str) -> PendingInteraction | None:
        value = self._store.get_pending_interaction(request_id, agent=AgentKind.CODEX)
        return _interaction_from_agent(value) if value else None

    def get_pending_interaction_by_lark_message_id(self, message_id: str) -> PendingInteraction | None:
        value = self._store.get_pending_interaction_by_lark_message_id(message_id, agent=AgentKind.CODEX)
        return _interaction_from_agent(value) if value else None

    def attach_lark_message_id(self, interaction_id: str, message_id: str) -> bool:
        return self._store.attach_lark_message_id(interaction_id, message_id, agent=AgentKind.CODEX)

    def resolve_interaction(
        self,
        interaction_id: str,
        *,
        decision: str,
        actor_id: str,
        resolved_at: datetime | None = None,
    ) -> bool:
        return self._store.resolve_interaction(
            interaction_id,
            decision=decision,
            actor_id=actor_id,
            resolved_at=resolved_at,
            agent=AgentKind.CODEX,
        )

    def resolve_interaction_and_refresh_session(
        self,
        interaction_id: str,
        *,
        decision: str,
        actor_id: str,
        updated_at: datetime,
        status: InteractionStatus = InteractionStatus.RESOLVED,
    ) -> bool:
        return self._store.resolve_interaction_and_refresh_session(
            interaction_id,
            decision=decision,
            actor_id=actor_id,
            updated_at=updated_at,
            status=SharedInteractionStatus(status.value),
            agent=AgentKind.CODEX,
        )

    def cancel_interaction_and_refresh_session(self, interaction_id: str, *, updated_at: datetime) -> bool:
        return self._store.cancel_interaction_and_refresh_session(
            interaction_id, updated_at=updated_at, agent=AgentKind.CODEX
        )

    def expire_interaction(self, interaction_id: str, *, resolved_at: datetime | None = None) -> bool:
        return self._store.expire_interaction(
            interaction_id, resolved_at=resolved_at, agent=AgentKind.CODEX
        )

    def cancel_pending_interactions(
        self,
        session_id: str,
        *,
        status: InteractionStatus,
        resolved_at: datetime | None = None,
    ) -> list[str]:
        return self._store.cancel_pending_interactions(
            session_id,
            status=SharedInteractionStatus(status.value),
            resolved_at=resolved_at,
            agent=AgentKind.CODEX,
        )

    def claim_session_terminal(
        self,
        session_id: str,
        terminal_status: SessionStatus,
        summary: str,
        pending_status: InteractionStatus,
        updated_at: datetime,
    ) -> list[str] | None:
        return self._store.claim_session_terminal(
            session_id,
            SharedSessionStatus(terminal_status.value),
            summary,
            SharedInteractionStatus(pending_status.value),
            updated_at,
            agent=AgentKind.CODEX,
        )

    def finish_interactive_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        summary: str,
        updated_at: datetime,
    ) -> list[str] | None:
        return self._store.finish_interactive_turn(
            session_id,
            turn_id=turn_id,
            summary=summary,
            updated_at=updated_at,
            agent=AgentKind.CODEX,
        )

    def record_event_once(self, event_id: str, *, received_at: datetime | None = None) -> bool:
        return self._store.record_event_once(event_id, received_at=received_at, agent=AgentKind.CODEX)

    def enqueue_outbox(
        self,
        *,
        notification_type: str,
        payload_summary: str,
        session_id: str | None = None,
        agent: AgentKind | str | None = AgentKind.CODEX,
        session_name: str | None = None,
        interaction_id: str | None = None,
        next_attempt_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> int:
        if agent is not None and AgentKind(agent) is not AgentKind.CODEX:
            raise ValueError("SQLiteCodexStore only accepts codex outbox rows")
        return self._store.enqueue_outbox(
            notification_type=notification_type,
            payload_summary=payload_summary,
            session_id=session_id,
            agent=AgentKind.CODEX,
            session_name=session_name,
            interaction_id=interaction_id,
            next_attempt_at=next_attempt_at,
            created_at=created_at,
        )

    def get_outbox_item(self, outbox_id: int) -> NotificationOutboxItem | None:
        value = self._store.get_outbox_item(outbox_id, agent=AgentKind.CODEX)
        return NotificationOutboxItem.model_validate(value.model_dump()) if value else None

    def list_due_outbox(self, *, now: datetime | None = None, limit: int = 100) -> list[NotificationOutboxItem]:
        values = self._store.list_due_outbox(now=now, limit=limit, agent=AgentKind.CODEX)
        return [NotificationOutboxItem.model_validate(value.model_dump()) for value in values]

    def mark_outbox_sent(self, outbox_id: int, *, sent_at: datetime | None = None) -> bool:
        return self._store.mark_outbox_sent(outbox_id, sent_at=sent_at, agent=AgentKind.CODEX)

    def record_outbox_failure(self, outbox_id: int, *, error: str, next_attempt_at: datetime) -> bool:
        return self._store.record_outbox_failure(
            outbox_id, error=error, next_attempt_at=next_attempt_at, agent=AgentKind.CODEX
        )

    def reconcile_startup(self, *, now: datetime | None = None) -> StartupReconciliationResult:
        value = self._store.reconcile_startup(now=now, agent=AgentKind.CODEX)
        return StartupReconciliationResult.model_validate(value.model_dump())

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
        return self._store.record_audit(
            event_type=event_type,
            detail_summary=detail_summary,
            session_id=session_id,
            interaction_id=interaction_id,
            actor_id=actor_id,
            created_at=created_at,
            agent=AgentKind.CODEX,
        )

    def list_audit(self, *, session_id: str | None = None) -> list[CodexAuditEntry]:
        values = self._store.list_audit(session_id=session_id, agent=AgentKind.CODEX)
        return [CodexAuditEntry.model_validate(value.model_dump()) for value in values]

    @contextmanager
    def _connection(self) -> Iterator[object]:
        with self._store._connection() as connection:
            yield connection

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> Self:
        self._store.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._store.__exit__(exc_type, exc_value, traceback)
