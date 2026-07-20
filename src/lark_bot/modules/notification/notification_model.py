from __future__ import annotations

from pydantic import BaseModel, Field

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.task.task_model import DetectionResult, TaskResult, TaskStatus


class AgentNotificationInput(BaseModel):
    agent: AgentKind
    task_name: str = Field(min_length=1)
    status: TaskStatus
    command: list[str]
    session_id: str | None = None
    session_name: str | None = None
    event_name: str | None = None
    event_id: str | None = None
    exit_code: int | None = None
    duration_seconds: float = 0
    summary: str = ""
    output_tail: list[str] = Field(default_factory=list)
    stderr_tail: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class NotificationContext(BaseModel):
    agent: AgentKind
    session_id: str
    session_name: str


class NotificationRequest(BaseModel):
    task: TaskResult
    detection: DetectionResult
    context: NotificationContext | None = None
    event_id: str | None = None

    @property
    def dedupe_key(self) -> str:
        if self.event_id:
            return f"{self.task.source}:{self.event_id}"
        command_text = " ".join(self.task.command)
        session = self.context.session_id if self.context else "-"
        return f"{self.task.source}:{session}:{self.task.name}:{command_text}:{self.detection.status}"
