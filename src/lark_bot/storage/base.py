from __future__ import annotations

from datetime import datetime
from typing import Protocol


class NotificationStore(Protocol):
    def should_send(self, dedupe_key: str, cooldown_seconds: int, now: datetime | None = None) -> bool:
        """Return whether a notification should be sent."""

    def record_sent(self, dedupe_key: str, status: str, now: datetime | None = None) -> None:
        """Record that a notification was sent."""
