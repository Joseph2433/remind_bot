from __future__ import annotations

import json
import logging

from pydantic import ValidationError
import typer

from lark_bot.core.logging import configure_logging
from lark_bot.config import Settings
from lark_bot.lark.client import LarkBotClient
from lark_bot.models import NotificationRequest
from lark_bot.notifications.adapters.codex import CodexEvent, codex_event_to_notification
from lark_bot.storage.sqlite import SQLiteNotificationStore


def send_with_dedupe(request: NotificationRequest, settings: Settings) -> None:
    validate_lark_settings(settings)
    store = SQLiteNotificationStore(settings.sqlite_path)
    if not store.should_send(request.dedupe_key, settings.cooldown_seconds):
        logging.getLogger(__name__).info("Notification suppressed by cooldown.")
        return
    client = LarkBotClient(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret,
        receive_id=settings.lark_receive_id,
        receive_id_type=settings.lark_receive_id_type,
        base_url=settings.lark_base_url,
        timeout_seconds=settings.http_timeout_seconds,
        message_format=settings.message_format,
        output_tail_lines=settings.output_tail_lines,
    )
    try:
        client.send(request)
    finally:
        client.close()
    store.record_sent(request.dedupe_key, request.detection.status.value)


def validate_lark_settings(settings: Settings) -> None:
    missing = [
        name
        for name, value in {
            "LARK_BOT_LARK_APP_ID": settings.lark_app_id,
            "LARK_BOT_LARK_APP_SECRET": settings.lark_app_secret,
            "LARK_BOT_LARK_RECEIVE_ID": settings.lark_receive_id,
        }.items()
        if not value
    ]
    if missing:
        raise typer.BadParameter(f"Missing required Lark settings: {', '.join(missing)}")


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
