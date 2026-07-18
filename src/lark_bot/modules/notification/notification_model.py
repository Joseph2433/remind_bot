from __future__ import annotations

from pydantic import BaseModel

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.task.task_model import DetectionResult, TaskResult


class NotificationContext(BaseModel):
    agent: AgentKind
    session_id: str
    session_name: str


class NotificationRequest(BaseModel):
    task: TaskResult
    detection: DetectionResult
    context: NotificationContext | None = None

    @property
    def dedupe_key(self) -> str:
        command_text = " ".join(self.task.command)
        session = self.context.session_id if self.context else "-"
        return f"{self.task.source}:{session}:{self.task.name}:{command_text}:{self.detection.status}"
