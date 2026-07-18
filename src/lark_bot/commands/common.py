from __future__ import annotations

import typer

from lark_bot.core.logging import configure_logging
from lark_bot.core.config import Settings
from lark_bot.modules.agent.agent_event import parse_event_payload
from lark_bot.modules.notification.notification_model import NotificationRequest
from lark_bot.modules.notification.notification_service import (
    send_with_dedupe,
    validate_lark_settings,
)
from lark_bot.modules.codex.codex_adapter import CodexEvent, codex_event_to_notification


def build_codex_notification_from_json(payload: str) -> NotificationRequest:
    event = parse_event_payload(payload, CodexEvent, provider="Codex")
    return codex_event_to_notification(event)
