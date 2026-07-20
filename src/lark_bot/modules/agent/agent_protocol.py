from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from lark_bot.modules.agent.agent_model import AgentKind, AgentSession


class AgentAdapter(Protocol):
    agent: AgentKind

    async def start(self) -> None:
        """Start provider-level resources shared by its sessions."""

    async def close(self) -> None:
        """Release provider-level resources."""

    async def create_session(
        self,
        name: str,
        cwd: str,
        prompt: str,
        **options: Any,
    ) -> AgentSession:
        """Create one independent provider session."""

    async def cancel_session(self, session_id: str) -> bool:
        """Cancel one provider session and report whether it was active."""

    async def list_sessions(self, status: Any = None) -> list[AgentSession]: ...

    async def get_session(self, session_id: str) -> AgentSession | None: ...

    async def resolve_interaction(self, interaction_id: str, actor_id: str, **kwargs: Any) -> bool: ...

    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]: ...

    async def expire_due_interactions(self, now: Any = None) -> list[str]: ...


SessionOperation = Callable[..., Awaitable[Any]]
