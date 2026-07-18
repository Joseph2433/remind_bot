from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from lark_bot.modules.agent.agent_model import AgentKind, AgentSession
from lark_bot.modules.claude.claude_adapter import claude_event_to_notification
from lark_bot.modules.claude.claude_model import ClaudeEvent
from lark_bot.modules.notification.notification_model import NotificationRequest


def build_claude_notification_from_json(payload: str) -> NotificationRequest:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError("Claude event payload must be valid JSON.") from error
    if not isinstance(raw, dict):
        raise ValueError("Claude event payload must be a JSON object.")
    try:
        event = ClaudeEvent.model_validate(raw)
    except ValidationError as error:
        raise ValueError(f"Invalid Claude event payload: {error}") from error
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
