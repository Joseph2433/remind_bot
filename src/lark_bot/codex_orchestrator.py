from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from lark_bot.codex_app_server import (
    ServerNotification,
    ServerRequest,
    command_approval_response,
    file_approval_response,
    permission_response,
    user_input_response,
)
from lark_bot.codex_models import (
    CodexSession,
    InteractionKind,
    InteractionStatus,
    PendingInteraction,
    SessionStatus,
)
from lark_bot.redaction import redact_text


_SUMMARY_LIMIT = 2000
_ACTIVE_STATUSES = frozenset(
    {
        SessionStatus.STARTING,
        SessionStatus.RUNNING,
        SessionStatus.WAITING_FOR_APPROVAL,
        SessionStatus.WAITING_FOR_INPUT,
    }
)
_REQUEST_KINDS = {
    "item/commandExecution/requestApproval": InteractionKind.EXEC_APPROVAL,
    "item/fileChange/requestApproval": InteractionKind.FILE_CHANGE_APPROVAL,
    "item/permissions/requestApproval": InteractionKind.PERMISSION_REQUEST,
    "item/tool/requestUserInput": InteractionKind.USER_INPUT,
}


class OrchestratorEventType(StrEnum):
    SESSION_STARTED = "session_started"
    INTERACTION_REQUESTED = "interaction_requested"
    INTERACTION_RESOLVED = "interaction_resolved"
    SESSION_COMPLETED = "session_completed"
    SESSION_INTERRUPTED = "session_interrupted"


@dataclass(frozen=True, slots=True)
class OrchestratorEvent:
    event_type: OrchestratorEventType
    session_id: str
    interaction_id: str | None
    status: SessionStatus
    summary: str


@dataclass(frozen=True, slots=True)
class _LiveInteraction:
    request: ServerRequest
    interaction: PendingInteraction


class CodexOrchestrator:
    def __init__(
        self,
        store: Any,
        app_server: Any,
        *,
        now: Callable[[], datetime],
        id_factory: Callable[[], str],
        interaction_timeout_seconds: int = 1800,
        event_queue_capacity: int = 100,
    ) -> None:
        if interaction_timeout_seconds <= 0:
            raise ValueError("interaction timeout must be positive")
        if event_queue_capacity <= 0:
            raise ValueError("event queue capacity must be positive")
        self._store = store
        self._app_server = app_server
        self._now = now
        self._id_factory = id_factory
        self._interaction_timeout = timedelta(seconds=interaction_timeout_seconds)
        self.events: asyncio.Queue[OrchestratorEvent] = asyncio.Queue(
            maxsize=event_queue_capacity
        )
        self._live: dict[str, _LiveInteraction] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._closed = False
        self._terminal_error: BaseException | None = None
        self._final_text: dict[tuple[str, str], tuple[str, bool]] = {}

    @property
    def terminal_error(self) -> BaseException | None:
        return self._terminal_error

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("CodexOrchestrator is closed")
            await self._app_server.start()
            reconciliation = self._store.reconcile_startup(now=self._utc_now())
            for session_id in reconciliation.session_ids:
                await self._emit(
                    OrchestratorEventType.SESSION_INTERRUPTED,
                    session_id,
                    status=SessionStatus.INTERRUPTED,
                    summary="interrupted during daemon restart",
                )
            self._tasks = [
                asyncio.create_task(
                    self._consume_requests(), name="codex-orchestrator-requests"
                ),
                asyncio.create_task(
                    self._consume_notifications(),
                    name="codex-orchestrator-notifications",
                ),
                asyncio.create_task(
                    self._monitor_app_server(), name="codex-orchestrator-monitor"
                ),
            ]
            self._started = True

    async def create_session(
        self,
        name: str,
        cwd: str,
        prompt: str,
        model: str | None = None,
        sandbox: str = "workspace-write",
    ) -> CodexSession:
        timestamp = self._utc_now()
        session_id = str(self._id_factory())
        session = CodexSession(
            id=session_id,
            name=name,
            cwd=cwd,
            model=model,
            sandbox=sandbox,
            status=SessionStatus.STARTING,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._store.create_session(session)
        try:
            thread_id = await self._app_server.start_thread(cwd, model, sandbox)
            if getattr(self._app_server, "is_running", True) is False:
                raise RuntimeError("Codex app-server stopped during startup")
            if not self._store.update_session_if_status(
                session_id,
                (SessionStatus.STARTING,),
                thread_id=thread_id,
                status=SessionStatus.RUNNING,
                updated_at=self._utc_now(),
            ):
                raise RuntimeError("session stopped during startup")
            turn_id = await self._app_server.start_turn(thread_id, prompt)
            if getattr(self._app_server, "is_running", True) is False:
                raise RuntimeError("Codex app-server stopped during startup")
            if not self._store.update_session_if_status(
                session_id,
                _ACTIVE_STATUSES,
                turn_id=turn_id,
                status=SessionStatus.RUNNING,
                updated_at=self._utc_now(),
            ):
                raise RuntimeError("session stopped during startup")
            current = self._store.get_session(session_id)
            assert current is not None
            await self._emit(
                OrchestratorEventType.SESSION_STARTED,
                session_id,
                status=SessionStatus.RUNNING,
                summary="session started",
            )
            return current
        except BaseException as error:
            summary = f"Codex session startup failed ({type(error).__name__})"
            won = self._store.claim_session_terminal(
                session_id,
                SessionStatus.FAILED,
                summary=summary,
                pending_status=InteractionStatus.CANCELLED,
                updated_at=self._utc_now(),
            )
            if won is not None:
                await self._emit(OrchestratorEventType.SESSION_COMPLETED, session_id, status=SessionStatus.FAILED, summary=summary)
            raise

    async def process_server_request(
        self, request: ServerRequest
    ) -> PendingInteraction | None:
        kind = _REQUEST_KINDS.get(request.method)
        if kind is None:
            await self._app_server.respond_error(
                request.request_id, -32601, "method not found"
            )
            return None
        thread_id = request.params.get("threadId")
        session = (
            self._store.get_session_by_thread(thread_id)
            if isinstance(thread_id, str) and thread_id
            else None
        )
        if session is None or session.status not in _ACTIVE_STATUSES:
            await self._app_server.respond_error(
                request.request_id, -32602, "invalid or inactive threadId"
            )
            return None

        timestamp = self._utc_now()
        interaction = PendingInteraction(
            id=str(self._id_factory()),
            session_id=session.id,
            request_id=_canonical_request_id(request.request_id),
            kind=kind,
            payload_summary=_request_summary(request.params),
            requested_at=timestamp,
            expires_at=timestamp + self._interaction_timeout,
        )
        waiting_status = (
            SessionStatus.WAITING_FOR_INPUT
            if kind is InteractionKind.USER_INPUT
            else SessionStatus.WAITING_FOR_APPROVAL
        )
        if not self._store.create_interaction_and_mark_waiting(interaction, waiting_status, self._utc_now()):
            await self._app_server.respond_error(request.request_id, -32600, "duplicate or inactive request")
            return None
        self._live[interaction.id] = _LiveInteraction(request, interaction)
        await self._emit(
            OrchestratorEventType.INTERACTION_REQUESTED,
            session.id,
            interaction_id=interaction.id,
            status=waiting_status,
            summary=interaction.payload_summary,
        )
        return interaction

    async def resolve_interaction(
        self,
        interaction_id: str,
        actor_id: str,
        *,
        allow: bool | None = None,
        answers: Mapping[str, str] | None = None,
    ) -> bool:
        live = self._live.get(interaction_id)
        if live is None:
            return False
        interaction = live.interaction
        response, decision = self._resolution(interaction.kind, live.request, allow, answers)
        if not self._store.resolve_interaction_and_refresh_session(
            interaction_id,
            decision=decision,
            actor_id=actor_id,
            updated_at=self._utc_now(),
        ):
            return False
        self._live.pop(interaction_id, None)
        try:
            await self._app_server.respond(live.request.request_id, response)
        except BaseException as error:
            await self._interrupt_session(
                interaction.session_id, _safe_summary(str(error))
            )
            raise
        current = self._store.get_session(interaction.session_id)
        if current is not None and current.status in _ACTIVE_STATUSES:
            await self._emit(
                OrchestratorEventType.INTERACTION_RESOLVED,
                interaction.session_id,
                interaction_id=interaction_id,
                status=SessionStatus.RUNNING,
                summary=decision,
            )
        return True

    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]:
        live = self._live.get(interaction_id)
        if live is None or live.interaction.kind is not InteractionKind.USER_INPUT:
            return ()
        questions = live.request.params.get("questions")
        if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
            return ()
        question_ids: list[str] = []
        for question in questions:
            if not isinstance(question, Mapping):
                return ()
            question_id = question.get("id")
            if not isinstance(question_id, str) or not question_id:
                return ()
            question_ids.append(question_id)
        return tuple(question_ids)

    async def process_notification(self, notification: ServerNotification) -> None:
        if notification.method == "item/completed":
            self._remember_final_text(notification.params)
            return
        if notification.method == "turn/started":
            thread_id = notification.params.get("threadId")
            turn = notification.params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            if not isinstance(thread_id, str) or not isinstance(turn_id, str):
                return
            session = self._store.get_session_by_thread(thread_id)
            if session is None or session.status not in _ACTIVE_STATUSES:
                return
            self._store.update_session_if_status(
                session.id,
                _ACTIVE_STATUSES,
                turn_id=turn_id,
                status=SessionStatus.RUNNING,
                updated_at=self._utc_now(),
            )
            return
        if notification.method != "turn/completed":
            return
        thread_id = notification.params.get("threadId")
        turn = notification.params.get("turn")
        if not isinstance(thread_id, str) or not isinstance(turn, Mapping):
            return
        session = self._store.get_session_by_thread(thread_id)
        if session is None or session.status not in _ACTIVE_STATUSES:
            return
        status = _terminal_status(turn.get("status"))
        if status is None:
            return
        summary = _turn_summary(notification.params, turn)
        turn_id = turn.get("id")
        remembered = self._final_text.pop((thread_id, turn_id), None) if isinstance(turn_id, str) else None
        if remembered is not None:
            summary = remembered[0]
        affected = self._store.claim_session_terminal(session.id, status, summary=summary, pending_status=InteractionStatus.CANCELLED, updated_at=self._utc_now())
        if affected is None:
            return
        self._drop_live_for_session(session.id)
        event_type = (
            OrchestratorEventType.SESSION_INTERRUPTED
            if status is SessionStatus.INTERRUPTED
            else OrchestratorEventType.SESSION_COMPLETED
        )
        await self._emit(
            event_type, session.id, status=status, summary=summary
        )

    async def expire_due_interactions(
        self, now: datetime | None = None
    ) -> list[str]:
        current_time = _as_utc(now or self._now())
        expired: list[str] = []
        for interaction_id, live in sorted(self._live.items()):
            interaction = live.interaction
            if interaction.expires_at > current_time:
                continue
            decision = "submitted" if interaction.kind is InteractionKind.USER_INPUT else _denial_decision(interaction.kind)
            if not self._store.resolve_interaction_and_refresh_session(
                interaction_id, decision=decision, actor_id="timeout", updated_at=current_time, status=InteractionStatus.EXPIRED
            ):
                continue
            self._live.pop(interaction_id, None)
            expired.append(interaction_id)
            if interaction.kind is InteractionKind.USER_INPUT:
                session = self._store.get_session(interaction.session_id)
                interrupt_error: BaseException | None = None
                if session is not None and session.thread_id and session.turn_id:
                    try:
                        await self._app_server.interrupt_turn(
                            session.thread_id, session.turn_id
                        )
                    except BaseException as error:
                        interrupt_error = error
                await self._interrupt_session(
                    interaction.session_id, "user input request expired"
                )
                if interrupt_error is not None:
                    raise interrupt_error
                continue
            response = self._denial_response(interaction.kind, live.request.params)
            try:
                await self._app_server.respond(live.request.request_id, response)
            except BaseException as error:
                await self._interrupt_session(
                    interaction.session_id, _safe_summary(str(error))
                )
                raise
            session = self._store.get_session(interaction.session_id)
            if session is not None and session.status in _ACTIVE_STATUSES:
                await self._emit(
                OrchestratorEventType.INTERACTION_RESOLVED,
                interaction.session_id,
                interaction_id=interaction_id,
                status=session.status,
                summary="expired and denied",
            )
        return expired

    async def cancel_session(self, session_id: str) -> bool:
        session = self._store.get_session(session_id)
        if session is None or session.status not in _ACTIVE_STATUSES:
            return False
        timestamp = self._utc_now()
        affected = self._store.claim_session_terminal(session_id, SessionStatus.CANCELLED, summary="cancelled", pending_status=InteractionStatus.CANCELLED, updated_at=timestamp)
        if affected is None:
            return False
        self._drop_live_for_session(session_id)
        await self._emit(
            OrchestratorEventType.SESSION_COMPLETED,
            session_id,
            status=SessionStatus.CANCELLED,
            summary="cancelled",
        )
        if session.thread_id and session.turn_id:
            try:
                await self._app_server.interrupt_turn(session.thread_id, session.turn_id)
            except Exception as error:
                self._terminal_error = error
        return True

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            for task in self._tasks:
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            try:
                await self._app_server.close()
            except BaseException as error:
                self._terminal_error = error
                raise
            finally:
                self._closed = True

    async def _consume_requests(self) -> None:
        while True:
            request = await self._app_server.requests.get()
            try:
                await self.process_server_request(request)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                await self._consumer_failed(error)
                return

    async def _consume_notifications(self) -> None:
        while True:
            notification = await self._app_server.notifications.get()
            try:
                await self.process_notification(notification)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                await self._consumer_failed(error)
                return

    async def _monitor_app_server(self) -> None:
        try:
            await self._app_server.wait_closed()
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            summary = _safe_summary(str(error))
        else:
            summary = "Codex app-server closed unexpectedly"
        if self._closing:
            return
        for session in self._store.list_sessions():
            if session.status in _ACTIVE_STATUSES:
                await self._interrupt_session(session.id, summary)

    async def _consumer_failed(self, error: BaseException) -> None:
        self._terminal_error = error
        summary = f"Codex orchestrator consumer failed ({type(error).__name__})"
        for session in self._store.list_sessions():
            if session.status in _ACTIVE_STATUSES:
                await self._interrupt_session(session.id, summary)
        self._closing = True
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        try:
            await self._app_server.close()
        except BaseException:
            pass

    def _remember_final_text(self, params: Mapping[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item = params.get("item")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str) or not isinstance(item, Mapping):
            return
        if item.get("type") != "agentMessage":
            return
        text = item.get("text")
        if not isinstance(text, str):
            return
        phase = item.get("phase")
        is_final = phase == "final_answer"
        previous = self._final_text.get((thread_id, turn_id))
        if previous is None or is_final or not previous[1]:
            self._final_text[(thread_id, turn_id)] = (_safe_summary(text), is_final)

    async def _interrupt_session(self, session_id: str, summary: str) -> bool:
        session = self._store.get_session(session_id)
        if session is None or session.status not in _ACTIVE_STATUSES:
            return False
        timestamp = self._utc_now()
        affected = self._store.claim_session_terminal(session_id, SessionStatus.INTERRUPTED, summary=summary, pending_status=InteractionStatus.CANCELLED, updated_at=timestamp)
        if affected is None:
            return False
        self._drop_live_for_session(session_id)
        await self._emit(
            OrchestratorEventType.SESSION_INTERRUPTED,
            session_id,
            status=SessionStatus.INTERRUPTED,
            summary=summary,
        )
        return True

    def _resolution(
        self,
        kind: InteractionKind,
        request: ServerRequest,
        allow: bool | None,
        answers: Mapping[str, str] | None,
    ) -> tuple[object, str]:
        if kind is InteractionKind.USER_INPUT:
            if answers is None:
                raise ValueError("answers are required for user input")
            questions = request.params.get("questions")
            if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
                raise ValueError("questions must be an array")
            response = user_input_response(questions, answers)
            return response, "submitted"
        if allow is None:
            raise ValueError("allow is required for approvals")
        if kind is InteractionKind.EXEC_APPROVAL:
            return command_approval_response(allow), "approved" if allow else "denied"
        if kind is InteractionKind.FILE_CHANGE_APPROVAL:
            return file_approval_response(allow), "accept" if allow else "decline"
        if kind is InteractionKind.PERMISSION_REQUEST:
            return permission_response(request.params, allow), "granted" if allow else "denied"
        raise ValueError(f"unsupported interaction kind: {kind}")

    @staticmethod
    def _denial_response(
        kind: InteractionKind, params: Mapping[str, Any]
    ) -> object:
        if kind is InteractionKind.EXEC_APPROVAL:
            return command_approval_response(False)
        if kind is InteractionKind.FILE_CHANGE_APPROVAL:
            return file_approval_response(False)
        if kind is InteractionKind.PERMISSION_REQUEST:
            return permission_response(params, False)
        raise ValueError("user input does not have a denial response")

    def _drop_live_for_session(self, session_id: str) -> None:
        for interaction_id in [
            interaction_id
            for interaction_id, live in self._live.items()
            if live.interaction.session_id == session_id
        ]:
            self._live.pop(interaction_id, None)

    async def _emit(
        self,
        event_type: OrchestratorEventType,
        session_id: str,
        *,
        status: SessionStatus,
        summary: str,
        interaction_id: str | None = None,
    ) -> None:
        event = OrchestratorEvent(
                event_type=event_type,
                session_id=session_id,
                interaction_id=interaction_id,
                status=status,
                summary=_safe_summary(summary),
            )
        self._store.enqueue_outbox(notification_type=f"orchestrator:{event_type.value}", payload_summary=event.summary, session_id=session_id, interaction_id=interaction_id, created_at=self._utc_now())
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def _utc_now(self) -> datetime:
        return _as_utc(self._now())


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_summary(value: str) -> str:
    return redact_text(value)[:_SUMMARY_LIMIT]


def _canonical_request_id(value: int | str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _denial_decision(kind: InteractionKind) -> str:
    if kind is InteractionKind.EXEC_APPROVAL:
        return "denied"
    if kind is InteractionKind.FILE_CHANGE_APPROVAL:
        return "decline"
    if kind is InteractionKind.PERMISSION_REQUEST:
        return "denied"
    raise ValueError("user input has no denial decision")


def _request_summary(params: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in ("reason", "command", "path"):
        value = params.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values.extend(str(item) for item in value if isinstance(item, str))
    questions = params.get("questions")
    if isinstance(questions, Sequence) and not isinstance(questions, (str, bytes)):
        for question in questions:
            if not isinstance(question, Mapping):
                continue
            for key in ("question", "header", "prompt"):
                value = question.get(key)
                if isinstance(value, str):
                    values.append(value)
    return _safe_summary(" | ".join(values))


def _terminal_status(value: object) -> SessionStatus | None:
    return {
        "completed": SessionStatus.SUCCEEDED,
        "failed": SessionStatus.FAILED,
        "interrupted": SessionStatus.INTERRUPTED,
    }.get(value)


def _turn_summary(params: Mapping[str, Any], turn: Mapping[str, Any]) -> str:
    for container in (turn, params):
        error = container.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return _safe_summary(error["message"])
    for container in (turn, params):
        for key in ("finalResponse", "final_response", "text", "outputText"):
            value = container.get(key)
            if isinstance(value, str):
                return _safe_summary(value)
    return ""
