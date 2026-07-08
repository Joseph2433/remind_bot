from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from lack_bot.detector import detect_output
from lack_bot.models import DetectionResult, NotificationRequest, TaskResult, TaskStatus

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
    exit_code = _event_exit_code(event, status)
    task = TaskResult(
        name=event.task_name,
        command=event.command,
        exit_code=exit_code,
        duration_seconds=event.duration_seconds,
        stdout_tail=event.output_tail,
        stderr_tail=event.stderr_tail,
        source="codex",
    )
    detection = _codex_detection(event, task, status)
    return NotificationRequest(task=task, detection=detection)


def _codex_detection(event: CodexEvent, task: TaskResult, status: TaskStatus) -> DetectionResult:
    detected = detect_output(task.combined_tail_text, task.exit_code)
    tags = _dedupe(["codex", *event.tags])
    if status is TaskStatus.WAITING_FOR_INPUT or detected.status is TaskStatus.WAITING_FOR_INPUT:
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=_dedupe([*tags, *detected.tags]),
            matched_phrases=detected.matched_phrases,
        )
    return DetectionResult(status=status, tags=_dedupe([*tags, status.value]))


def _normalize_status(status: str) -> TaskStatus:
    value = status.strip().lower().replace("-", "_")
    if value in SUCCESS_STATUSES:
        return TaskStatus.SUCCEEDED
    if value in WAITING_STATUSES:
        return TaskStatus.WAITING_FOR_INPUT
    if value in FAILED_STATUSES:
        return TaskStatus.FAILED
    return TaskStatus.FAILED


def _event_exit_code(event: CodexEvent, status: TaskStatus) -> int:
    if event.exit_code is not None:
        return event.exit_code
    return 0 if status is TaskStatus.SUCCEEDED else 1


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
