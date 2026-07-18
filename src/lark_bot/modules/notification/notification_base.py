from __future__ import annotations

from typing import Protocol

from lark_bot.modules.notification.notification_model import NotificationRequest


class Notifier(Protocol):
    def send(self, request: NotificationRequest) -> None:
        """Send a notification request."""
