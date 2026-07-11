from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from lark_bot.detector import dedupe_tags, detect_output
from lark_bot.models import DetectionResult, NotificationRequest, TaskResult, TaskStatus

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
    if status is TaskStatus.SUCCEEDED and exit_code != 0:
        status = TaskStatus.FAILED
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
    tags = dedupe_tags(["codex", *event.tags])

    # Explicit waiting status wins; only keep phrase tags when output also looks waiting.
    if status is TaskStatus.WAITING_FOR_INPUT:
        if detected.status is TaskStatus.WAITING_FOR_INPUT:
            return DetectionResult(
                status=TaskStatus.WAITING_FOR_INPUT,
                tags=dedupe_tags([*tags, *detected.tags]),
                matched_phrases=detected.matched_phrases,
            )
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=dedupe_tags([*tags, TaskStatus.WAITING_FOR_INPUT.value]),
        )

    # Output-based waiting can still elevate success/failure terminals (approval prompts).
    if detected.status is TaskStatus.WAITING_FOR_INPUT:
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=dedupe_tags([*tags, *detected.tags]),
            matched_phrases=detected.matched_phrases,
        )

    return DetectionResult(status=status, tags=dedupe_tags([*tags, status.value]))


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


def _event_exit_code(event: CodexEvent, status: TaskStatus) -> int:
    if event.exit_code is not None:
        return event.exit_code
    if status is TaskStatus.FAILED:
        return 1
    return 0
