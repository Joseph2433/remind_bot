from __future__ import annotations

from typing import Protocol

from lack_bot.models import NotificationRequest


class Notifier(Protocol):
    def send(self, request: NotificationRequest) -> None:
        """Send a notification request."""
