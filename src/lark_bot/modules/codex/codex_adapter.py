from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.notification.notification_builder import build_agent_notification
from lark_bot.modules.notification.notification_model import AgentNotificationInput, NotificationRequest
from lark_bot.modules.task.task_model import TaskStatus

SUCCESS_STATUSES = {"success", "succeeded", "completed", "complete", "done"}
FAILED_STATUSES = {"failed", "failure", "error", "errored"}
WAITING_STATUSES = {
    "waiting",
    "waiting_for_input",
    "needs_input",
    "need_user_input",
    "approval_required",
    "permission_required",
    "blocked",
}


class CodexEvent(BaseModel):
    task_name: str = "codex task"
    session_id: str | None = None
    session_name: str | None = None
    status: str
    command: list[str] = Field(default_factory=lambda: ["codex"])
    exit_code: int | None = None
    duration_seconds: float = 0
    output_tail: list[str] = Field(default_factory=list)
    stderr_tail: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_common_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "task_name" not in normalized and "name" in normalized:
            normalized["task_name"] = normalized["name"]
        if "output_tail" not in normalized:
            for key in ("stdout_tail", "last_output", "message"):
                if key in normalized:
                    value = normalized[key]
                    normalized["output_tail"] = value if isinstance(value, list) else [str(value)]
                    break
        return normalized


def codex_event_to_notification(event: CodexEvent) -> NotificationRequest:
    status = _normalize_status(event.status)
    return build_agent_notification(
        AgentNotificationInput(
            agent=AgentKind.CODEX,
            task_name=event.task_name,
            session_id=event.session_id,
            session_name=event.session_name,
            status=status,
            command=event.command,
            exit_code=event.exit_code,
            duration_seconds=event.duration_seconds,
            output_tail=event.output_tail,
            stderr_tail=event.stderr_tail,
            tags=event.tags,
        )
    )


def _normalize_status(status: str) -> TaskStatus:
    value = status.strip().lower().replace("-", "_")
    if value in SUCCESS_STATUSES:
        return TaskStatus.SUCCEEDED
    if value in WAITING_STATUSES:
        return TaskStatus.WAITING_FOR_INPUT
    if value in FAILED_STATUSES:
        return TaskStatus.FAILED
    raise ValueError(
        f"Unsupported Codex status: {status!r}. "
        "Use a terminal alias such as completed, failed, or approval_required."
    )
