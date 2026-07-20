from __future__ import annotations

from typing import Any

from lark_bot.modules.agent.agent_event import parse_event_payload
from lark_bot.modules.agent.agent_model import AgentKind, AgentSession, SessionStatus
from lark_bot.modules.claude.claude_adapter import claude_event_to_notification
from lark_bot.modules.claude.claude_model import ClaudeEvent
from lark_bot.modules.notification.notification_model import NotificationRequest


def build_claude_notification_from_json(payload: str) -> NotificationRequest:
    event = parse_event_payload(payload, ClaudeEvent, provider="Claude")
    return claude_event_to_notification(event)


class ClaudeService:
    """Claude event provider and managed-session facade."""

    agent = AgentKind.CLAUDE

    def __init__(self, manager: Any | None = None) -> None:
        self.manager = manager

    async def start(self) -> None:
        if self.manager is not None:
            await self.manager.start()

    async def close(self) -> None:
        if self.manager is not None:
            await self.manager.close()

    async def create_session(
        self,
        name: str,
        cwd: str,
        prompt: str,
        **options: Any,
    ) -> AgentSession:
        if self.manager is None:
            raise RuntimeError("Claude managed sessions are not configured")
        return await self.manager.create_session(name, cwd, prompt, **options)

    async def cancel_session(self, session_id: str) -> bool:
        return bool(self.manager and await self.manager.cancel(session_id))

    async def list_sessions(self, status: SessionStatus | None = None) -> list[AgentSession]:
        return await self.manager.list_sessions(status) if self.manager is not None else []

    async def get_session(self, session_id: str) -> AgentSession | None:
        return await self.manager.get_session(session_id) if self.manager is not None else None

    async def resolve_interaction(self, interaction_id: str, actor_id: str, **kwargs: Any) -> bool:
        if self.manager is None:
            return False
        return await self.manager.resolve_interaction(interaction_id, actor_id, **kwargs)

    def get_user_input_question_ids(self, interaction_id: str) -> tuple[str, ...]:
        return self.manager.get_user_input_question_ids(interaction_id) if self.manager is not None else ()

    async def expire_due_interactions(self, now: Any = None) -> list[str]:
        return await self.manager.expire_due_interactions(now) if self.manager is not None else []
