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


SessionOperation = Callable[..., Awaitable[Any]]
