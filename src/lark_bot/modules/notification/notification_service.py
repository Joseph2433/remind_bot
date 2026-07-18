from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import typer

from lark_bot.modules.notification.notification_model import NotificationRequest
from lark_bot.storage.sqlite import SQLiteNotificationStore

if TYPE_CHECKING:
    from lark_bot.core.config import Settings


def send_with_dedupe(request: NotificationRequest, settings: Settings) -> None:
    from lark_bot.lark.client import LarkBotClient

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
