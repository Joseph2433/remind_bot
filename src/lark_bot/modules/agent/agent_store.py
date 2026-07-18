from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from lark_bot.modules.agent.agent_model import AgentSession


class AgentSessionStore(Protocol):
    def create(self, session: AgentSession) -> None:
        """Persist a new session."""

    def get(self, session_id: str) -> AgentSession | None:
        """Return one session by its stable ID."""

    def list(self) -> Iterable[AgentSession]:
        """Return all sessions in deterministic order."""

    def update(self, session: AgentSession) -> None:
        """Persist the current session state."""
