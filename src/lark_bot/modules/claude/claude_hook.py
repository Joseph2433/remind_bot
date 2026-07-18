from __future__ import annotations

from collections.abc import Callable

from lark_bot.modules.agent.agent_hook import MAX_HOOK_BYTES
from lark_bot.modules.claude.claude_hook_adapter import (
    handle_callback,
    normalize_callback,
    read_stdin_payload,
)
from lark_bot.modules.claude.claude_service import build_claude_notification_from_json
from lark_bot.modules.notification.notification_model import NotificationRequest


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


__all__ = [
    "handle_callback",
    "handle_hook",
    "normalize_callback",
    "read_hook_notification",
    "read_stdin_payload",
]
