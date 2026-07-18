from __future__ import annotations

from typing import Any

from lark_bot.modules.agent.agent_event import parse_event_payload
from lark_bot.modules.agent.agent_model import AgentKind, AgentSession
from lark_bot.modules.claude.claude_adapter import claude_event_to_notification
from lark_bot.modules.claude.claude_model import ClaudeEvent
from lark_bot.modules.notification.notification_model import NotificationRequest


def build_claude_notification_from_json(payload: str) -> NotificationRequest:
    event = parse_event_payload(payload, ClaudeEvent, provider="Claude")
    return claude_event_to_notification(event)


class ClaudeService:
    """Claude event provider; managed sessions are intentionally unsupported."""

    agent = AgentKind.CLAUDE

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def create_session(
        self,
        name: str,
        cwd: str,
        prompt: str,
        **options: Any,
    ) -> AgentSession:
        raise RuntimeError("Claude managed sessions are not supported")

    async def cancel_session(self, session_id: str) -> bool:
        return False
