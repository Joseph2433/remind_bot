from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from lark_bot.modules.agent.agent_model import (
    AgentInteraction,
    AgentKind,
    AgentSession,
    InteractionKind,
    InteractionStatus,
    SessionStatus,
)
from lark_bot.modules.agent.agent_mapper import safe_summary
from lark_bot.modules.agent.agent_store import AgentStoreContract
from lark_bot.modules.claude.claude_sdk import (
    ClaudePermissionResult,
    ClaudeSdkClient,
    ClaudeSdkClientFactory,
    ClaudeSdkMessage,
    ClaudeSdkOptions,
    ClaudeSdkResult,
)


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _summary(value: object, limit: int = 240) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return safe_summary(text)[:limit]


@dataclass
class _LiveInteraction:
    interaction: AgentInteraction
    future: asyncio.Future[ClaudePermissionResult]
    question_ids: tuple[str, ...] = ()


@dataclass
class _LiveSession:
    client: ClaudeSdkClient
    task: asyncio.Task[None] | None = None
    interactions: dict[str, _LiveInteraction] = field(default_factory=dict)
    cancel_requested: bool = False


class ClaudeSessionManager:
    """Lifecycle manager for independent Claude Agent SDK sessions."""

    agent = AgentKind.CLAUDE

    def __init__(
        self,
        store: AgentStoreContract,
        factory: ClaudeSdkClientFactory | None = None,
        *,
        sdk_client_factory: ClaudeSdkClientFactory | None = None,
        outbox: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        interaction_timeout_seconds: float = 1800,
        timeout_seconds: float | None = None,
        close_timeout_seconds: float = 5,
    ) -> None:
        factory = factory or sdk_client_factory
        if factory is None:
            raise TypeError("a Claude SDK client factory is required")
        if timeout_seconds is not None:
            interaction_timeout_seconds = timeout_seconds
        if interaction_timeout_seconds <= 0 or close_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        self._store = store
        self._factory = factory
        self._outbox = outbox or store
        self._clock = clock or now or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._interaction_timeout = float(interaction_timeout_seconds)
        self._close_timeout = float(close_timeout_seconds)
        self._live: dict[str, _LiveSession] = {}
        self._started = False
        self._closing = False
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("ClaudeSessionManager is closed")
            self._store.reconcile_startup(now=_utc(self._clock()), agent=self.agent)
            self._started = True

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            live = list(self._live.items())
            for session in self._store.list_sessions(agent=self.agent):
                if session.status in _ACTIVE_STATUSES:
                    self._claim_terminal(session.session_id, SessionStatus.INTERRUPTED, "manager closed")
            for _, entry in live:
                for interaction in entry.interactions.values():
                    if not interaction.future.done():
                        interaction.future.set_result(
                            ClaudePermissionResult(False, message="session closed")
                        )
                try:
                    await entry.client.interrupt()
                except BaseException:
                    pass
            tasks = [entry.task for _, entry in live if entry.task is not None]
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=self._close_timeout)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            for _, entry in live:
                try:
                    await entry.client.close()
                except BaseException:
                    pass
            self._live.clear()
            self._closed = True

    async def create_session(
        self,
        name: str,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        permission_mode: str | None = None,
        resume_id: str | None = None,
    ) -> AgentSession:
        if self._closing or self._closed:
            raise RuntimeError("ClaudeSessionManager is closing")
        timestamp = _utc(self._clock())
        session_id = str(self._id_factory())
        session = AgentSession(
            session_id=session_id,
            agent=self.agent,
            name=name,
            cwd=cwd,
            model=model,
            permission_mode=permission_mode,
            sandbox="workspace-write",
            conversation_id=resume_id,
            status=SessionStatus.STARTING,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._store.create_session(session)
        callback = self._permission_callback(session_id)
        client: ClaudeSdkClient | None = None
        try:
            sdk_options = ClaudeSdkOptions(
                cwd=cwd,
                model=model,
                permission_mode=permission_mode,
                resume=resume_id,
                session_id=session_id,
                can_use_tool=callback,
            )
            client = self._factory(sdk_options)
            await client.connect()
            if not self._store.update_session_if_status(
                session_id,
                (SessionStatus.STARTING,),
                status=SessionStatus.RUNNING,
                updated_at=_utc(self._clock()),
                agent=self.agent,
            ):
                raise RuntimeError("session stopped during startup")
            entry = _LiveSession(client)
            self._live[session_id] = entry
            entry.task = asyncio.create_task(
                self._run_session(session_id, client, prompt),
                name=f"claude-session-{session_id}",
            )
        except asyncio.CancelledError:
            self._claim_terminal(session_id, SessionStatus.INTERRUPTED, "Claude startup interrupted")
            self._live.pop(session_id, None)
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
            raise
        except Exception as error:
            self._claim_terminal(session_id, SessionStatus.FAILED, f"Claude startup failed ({type(error).__name__})")
            self._live.pop(session_id, None)
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
        return self._store.get_session(session_id, agent=self.agent) or session

    async def _run_session(self, session_id: str, client: ClaudeSdkClient, prompt: str) -> None:
        try:
            await client.query(prompt)
            result: ClaudeSdkResult | None = None
            async for message in client.receive_response():
                if isinstance(message, ClaudeSdkResult):
                    result = message
                    break
            if result is None:
                self._claim_terminal(session_id, SessionStatus.FAILED, "Claude stream ended without result")
                return
            status = SessionStatus.FAILED if result.is_error or result.subtype.lower() in {"error", "failed"} else SessionStatus.SUCCEEDED
            summary = _summary(result.result or (result.errors[0] if result.errors else result.subtype))
            self._claim_terminal(session_id, status, summary, conversation_id=result.session_id or None)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            status = SessionStatus.INTERRUPTED if self._closing else SessionStatus.FAILED
            self._claim_terminal(session_id, status, f"Claude stream failed ({type(error).__name__})")
        finally:
            entry = self._live.get(session_id)
            if entry is not None:
                try:
                    await client.close()
                except Exception:
                    pass
                if entry.task is asyncio.current_task():
                    self._live.pop(session_id, None)

    def _claim_terminal(
        self,
        session_id: str,
        status: SessionStatus,
        summary: str,
        *,
        conversation_id: str | None = None,
    ) -> bool:
        if conversation_id:
            try:
                self._store.update_session_if_status(
                    session_id,
                    _ACTIVE_STATUSES,
                    status=SessionStatus.RUNNING,
                    updated_at=_utc(self._clock()),
                    conversation_id=conversation_id,
                    agent=self.agent,
                )
            except (AttributeError, TypeError):
                pass
        try:
            claim = getattr(self._store, "claim_session_terminal", None)
            if claim is not None:
                won = claim(
                    session_id,
                    status,
                    _summary(summary),
                    InteractionStatus.CANCELLED,
                    _utc(self._clock()),
                    agent=self.agent,
                ) is not None
            else:
                won = self._store.update_session_if_status(
                session_id,
                _ACTIVE_STATUSES,
                status=status,
                summary=_summary(summary),
                updated_at=_utc(self._clock()),
                agent=self.agent,
                )
        except TypeError:
            won = self._store.update_session_if_status(
                session_id,
                _ACTIVE_STATUSES,
                status=status,
                summary=_summary(summary),
                updated_at=_utc(self._clock()),
                agent=self.agent,
            )
        if won:
            session = self._store.get_session(session_id, agent=self.agent)
            if session is not None:
                try:
                    self._outbox.enqueue_outbox(
                        notification_type="session_completed",
                        payload_summary=_summary(summary),
                        session_id=session_id,
                        agent=self.agent,
                        session_name=session.name,
                        created_at=_utc(self._clock()),
                    )
                except Exception:
                    pass
        return bool(won)

    async def wait_session(self, session_id: str) -> AgentSession | None:
        entry = self._live.get(session_id)
        if entry and entry.task:
            await asyncio.shield(entry.task)
        return self.get(session_id)

    def list(self, status: SessionStatus | None = None) -> list[AgentSession]:
        return self._store.list_sessions(status, agent=self.agent)

    async def list_sessions(self, status: SessionStatus | None = None) -> list[AgentSession]:
        return self.list(status)

    def get(self, session_id: str) -> AgentSession | None:
        return self._store.get_session(session_id, agent=self.agent)

    async def get_session(self, session_id: str) -> AgentSession | None:
        return self.get(session_id)

    async def cancel(self, session_id: str) -> bool:
        session = self.get(session_id)
        if session is None or session.status not in _ACTIVE_STATUSES:
            return False
        entry = self._live.get(session_id)
        if entry is not None:
            entry.cancel_requested = True
            won = self._claim_terminal(session_id, SessionStatus.CANCELLED, "cancelled")
            if not won:
                return False
            try:
                await entry.client.interrupt()
            except Exception:
                pass
            if entry.task is not None and not entry.task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(entry.task), self._close_timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            return won
        return self._claim_terminal(session_id, SessionStatus.CANCELLED, "cancelled")

    async def cancel_session(self, session_id: str) -> bool:
        return await self.cancel(session_id)

    async def resolve_interaction(
        self,
        interaction_id: str,
        actor_id: str,
        *,
        allow: bool | None = None,
        answers: Mapping[str, str] | None = None,
    ) -> bool:
        live = next((entry for entry in self._live.values() if interaction_id in entry.interactions), None)
        if live is None:
            return False
        item = live.interactions[interaction_id]
        kind = item.interaction.kind
        if kind is InteractionKind.USER_INPUT:
            if answers is None or any(key not in item.question_ids for key in answers):
                return False
            decision = "submitted"
            updated = {"questions": [{"question": key, "answer": str(value)} for key, value in answers.items()]}
            result = ClaudePermissionResult(True, updated_input=updated)
        else:
            if allow is None:
                return False
            decision = "granted" if allow else "denied"
            result = ClaudePermissionResult(bool(allow), message=None if allow else "Permission denied")
        won = self._store.resolve_interaction_and_refresh_session(
            interaction_id,
            decision=decision,
            actor_id=actor_id,
            updated_at=_utc(self._clock()),
            agent=self.agent,
        )
        if not won:
            return False
        live.interactions.pop(interaction_id, None)
        if not item.future.done():
            item.future.set_result(result)
        return True

    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]:
        for entry in self._live.values():
            item = entry.interactions.get(interaction_id)
            if item is not None:
                return item.question_ids
        return ()

    async def expire_due_interactions(self, now: datetime | None = None) -> list[str]:
        current = _utc(now or self._clock())
        expired: list[str] = []
        for entry in list(self._live.values()):
            for interaction_id, item in list(entry.interactions.items()):
                if item.interaction.expires_at > current:
                    continue
                if not self._store.resolve_interaction_and_refresh_session(
                    interaction_id,
                    decision="submitted" if item.interaction.kind is InteractionKind.USER_INPUT else "denied",
                    actor_id="timeout",
                    updated_at=current,
                    status=InteractionStatus.EXPIRED,
                    agent=self.agent,
                ):
                    continue
                entry.interactions.pop(interaction_id, None)
                if not item.future.done():
                    item.future.set_result(ClaudePermissionResult(False, message="interaction expired"))
                expired.append(interaction_id)
        return expired

    def _permission_callback(self, session_id: str):
        async def callback(tool_name: str, input_data: Mapping[str, Any], context: Mapping[str, Any] | None) -> ClaudePermissionResult:
            is_question = tool_name == "AskUserQuestion"
            kind = InteractionKind.USER_INPUT if is_question else InteractionKind.PERMISSION_REQUEST
            now = _utc(self._clock())
            interaction_id = str(self._id_factory())
            request_id = str(self._id_factory())
            question_ids = tuple(
                str(question.get("question", index))
                for index, question in enumerate(input_data.get("questions", []))
                if isinstance(question, Mapping)
            ) if is_question else ()
            payload = _summary(f"Tool permission: {tool_name}")
            interaction = AgentInteraction(
                interaction_id=interaction_id,
                session_id=session_id,
                request_id=request_id,
                kind=kind,
                payload_summary=payload,
                requested_at=now,
                expires_at=now + timedelta(seconds=self._interaction_timeout),
            )
            waiting = SessionStatus.WAITING_FOR_INPUT if is_question else SessionStatus.WAITING_FOR_APPROVAL
            if not self._store.create_interaction_and_mark_waiting(interaction, waiting, now, agent=self.agent):
                return ClaudePermissionResult(False, message="Permission request rejected")
            future: asyncio.Future[ClaudePermissionResult] = asyncio.get_running_loop().create_future()
            entry = self._live.get(session_id)
            if entry is None:
                self._store.resolve_interaction_and_refresh_session(
                    interaction_id,
                    decision="submitted" if is_question else "denied",
                    actor_id="system",
                    updated_at=_utc(self._clock()),
                    agent=self.agent,
                )
                return ClaudePermissionResult(False, message="session is not live")
            entry.interactions[interaction_id] = _LiveInteraction(interaction, future, question_ids)
            try:
                self._outbox.enqueue_outbox(
                    notification_type="user_input" if is_question else "permission_request",
                    payload_summary=payload,
                    session_id=session_id,
                    interaction_id=interaction_id,
                    agent=self.agent,
                    created_at=now,
                )
            except Exception:
                self._store.resolve_interaction_and_refresh_session(
                    interaction_id,
                    decision="submitted" if is_question else "denied",
                    actor_id="system",
                    updated_at=_utc(self._clock()),
                    agent=self.agent,
                )
                future.set_result(ClaudePermissionResult(False, message="delivery failure"))
            try:
                return await asyncio.wait_for(asyncio.shield(future), self._interaction_timeout)
            except asyncio.TimeoutError:
                self._store.resolve_interaction_and_refresh_session(
                    interaction_id,
                    decision="submitted" if is_question else "denied",
                    actor_id="timeout",
                    updated_at=_utc(self._clock()),
                    status=InteractionStatus.EXPIRED,
                    agent=self.agent,
                )
                entry.interactions.pop(interaction_id, None)
                return ClaudePermissionResult(False, message="interaction expired")
            finally:
                entry.interactions.pop(interaction_id, None)

        return callback


_ACTIVE_STATUSES = (
    SessionStatus.STARTING,
    SessionStatus.RUNNING,
    SessionStatus.WAITING,
    SessionStatus.WAITING_FOR_APPROVAL,
    SessionStatus.WAITING_FOR_INPUT,
)


class ClaudeSessionManagerContract(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def create_session(
        self,
        name: str,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        permission_mode: str | None = None,
        resume_id: str | None = None,
    ) -> AgentSession: ...
    async def list_sessions(self, status: SessionStatus | None = None) -> list[AgentSession]: ...
    async def get_session(self, session_id: str) -> AgentSession | None: ...
    async def cancel_session(self, session_id: str) -> bool: ...
    async def resolve_interaction(
        self,
        interaction_id: str,
        actor_id: str,
        *,
        allow: bool | None = None,
        answers: Mapping[str, str] | None = None,
    ) -> bool: ...
    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]: ...
    async def expire_due_interactions(self, now: datetime | None = None) -> list[str]: ...

__all__ = ["ClaudeSessionManager", "ClaudeSessionManagerContract"]
