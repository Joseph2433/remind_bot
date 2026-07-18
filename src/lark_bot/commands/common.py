from __future__ import annotations

import json

from pydantic import ValidationError
import typer

from lark_bot.core.logging import configure_logging
from lark_bot.core.config import Settings
from lark_bot.modules.notification.notification_model import NotificationRequest
from lark_bot.modules.notification.notification_service import (
    send_with_dedupe,
    validate_lark_settings,
)
from lark_bot.modules.codex.codex_adapter import CodexEvent, codex_event_to_notification


def build_codex_notification_from_json(payload: str) -> NotificationRequest:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Codex event payload must be valid JSON.") from exc
    if not isinstance(raw, dict):
        raise ValueError("Codex event payload must be a JSON object.")
    try:
        event = CodexEvent.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid Codex event payload: {exc}") from exc
    return codex_event_to_notification(event)
