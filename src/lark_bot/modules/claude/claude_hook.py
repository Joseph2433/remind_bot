from __future__ import annotations

from collections.abc import Callable

from lark_bot.modules.claude.claude_service import build_claude_notification_from_json
from lark_bot.modules.notification.notification_model import NotificationRequest

MAX_HOOK_BYTES = 64 * 1024


def read_hook_notification(payload: str) -> NotificationRequest:
    if len(payload.encode("utf-8")) > MAX_HOOK_BYTES:
        raise ValueError("Claude hook payload is too large")
    return build_claude_notification_from_json(payload)


def handle_hook(
    payload: str,
    sender: Callable[[NotificationRequest], None],
) -> NotificationRequest:
    request = read_hook_notification(payload)
    sender(request)
    return request
