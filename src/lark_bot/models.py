from __future__ import annotations

from typing import Literal

from lark_bot.modules.notification.notification_model import (
    NotificationContext,
    NotificationRequest,
)
from lark_bot.modules.task.task_model import DetectionResult, TaskResult, TaskStatus

ReceiveIdType = Literal["chat_id", "user_id", "open_id"]

__all__ = [
    "DetectionResult",
    "NotificationContext",
    "NotificationRequest",
    "ReceiveIdType",
    "TaskResult",
    "TaskStatus",
]
