from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.agent.agent_protocol import AgentAdapter

T = TypeVar("T")


@dataclass(slots=True)
class _SessionLock:
    lock: asyncio.Lock
    users: int = 0


class AgentSessionService:
    """Serialize operations per session while allowing unrelated sessions to run together."""

    def __init__(self) -> None:
        self._lock_guard = asyncio.Lock()
        self._locks: dict[str, _SessionLock] = {}

    async def run_serialized(
        self,
        session_id: str,
        operation: Callable[..., Awaitable[T]],
        *args: object,
        **kwargs: object,
    ) -> T:
        if not session_id:
            raise ValueError("session_id must not be empty")
        async with self._lock_guard:
            entry = self._locks.get(session_id)
            if entry is None:
                entry = _SessionLock(asyncio.Lock())
                self._locks[session_id] = entry
            entry.users += 1
        try:
            async with entry.lock:
                return await operation(*args, **kwargs)
        finally:
            async with self._lock_guard:
                entry.users -= 1
                if entry.users == 0 and not entry.lock.locked():
                    self._locks.pop(session_id, None)

    @staticmethod
    async def list_sessions(adapter: AgentAdapter, status: Any = None) -> list[Any]:
        return await adapter.list_sessions(status)

    @staticmethod
    async def get_session(adapter: AgentAdapter, session_id: str) -> Any:
        return await adapter.get_session(session_id)

    @staticmethod
    async def resolve_interaction(adapter: AgentAdapter, interaction_id: str, actor_id: str, **kwargs: Any) -> bool:
        return await adapter.resolve_interaction(interaction_id, actor_id, **kwargs)

    @staticmethod
    def get_user_input_question_ids(adapter: AgentAdapter, interaction_id: str) -> tuple[str, ...]:
        return adapter.get_user_input_question_ids(interaction_id)

    @staticmethod
    async def expire_due_interactions(adapter: AgentAdapter, now: Any = None) -> list[str]:
        return await adapter.expire_due_interactions(now)


class AgentRegistry:
    """Own one provider adapter per agent kind; sessions reuse those adapters."""

    def __init__(self) -> None:
        self._adapters: dict[AgentKind, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        agent = adapter.agent
        if agent in self._adapters:
            raise ValueError(f"agent adapter already registered: {agent.value}")
        self._adapters[agent] = adapter

    def get(self, agent: AgentKind | str) -> AgentAdapter:
        kind = agent if isinstance(agent, AgentKind) else AgentKind(agent)
        try:
            return self._adapters[kind]
        except KeyError as error:
            raise KeyError(f"agent adapter is not registered: {kind.value}") from error

    def registered(self) -> tuple[AgentKind, ...]:
        return tuple(self._adapters)


class AgentInteractionDispatcher:
    """Resolve an interaction through its canonical persisted session owner."""

    def __init__(self, store: Any, registry: AgentRegistry) -> None:
        self.store = store
        self.registry = registry

    async def resolve_interaction(
        self,
        interaction_id: str,
        actor_id: str,
        **resolution: object,
    ) -> bool:
        interaction = self.store.get_interaction(interaction_id)
        if interaction is None:
            return False
        session = self.store.get_session(interaction.session_id)
        if session is None:
            return False
        try:
            adapter = self.registry.get(session.agent)
        except (KeyError, ValueError):
            return False
        return bool(await adapter.resolve_interaction(interaction_id, actor_id, **resolution))

    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]:
        interaction = self.store.get_interaction(interaction_id)
        if interaction is None:
            return ()
        session = self.store.get_session(interaction.session_id)
        if session is None:
            return ()
        try:
            adapter = self.registry.get(session.agent)
        except (KeyError, ValueError):
            return ()
        return tuple(adapter.get_user_input_question_ids(interaction_id))
