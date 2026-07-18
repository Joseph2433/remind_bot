from __future__ import annotations

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.claude.claude_model import ClaudeEvent
from lark_bot.modules.notification.notification_model import (
    NotificationContext,
    NotificationRequest,
)
from lark_bot.modules.task.task_detector import dedupe_tags, detect_output
from lark_bot.modules.task.task_model import DetectionResult, TaskResult, TaskStatus

_SUPPORTED_EVENTS = frozenset({"stop", "permissionrequest", "userpromptsubmit"})
_WAITING_EVENTS = frozenset({"permissionrequest", "userpromptsubmit"})
_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "complete", "done"})
_FAILED_STATUSES = frozenset({"failed", "failure", "error", "errored"})


def claude_event_to_notification(event: ClaudeEvent) -> NotificationRequest:
    event_name = event.event_name.strip().casefold()
    if event_name not in _SUPPORTED_EVENTS:
        raise ValueError(f"Unsupported Claude event: {event.event_name!r}")

    status = _event_status(event, event_name)
    exit_code = event.exit_code
    if exit_code is None:
        exit_code = 1 if status is TaskStatus.FAILED else 0
    if status is TaskStatus.SUCCEEDED and exit_code != 0:
        status = TaskStatus.FAILED

    output_tail = event.output_tail or ([event.summary] if event.summary else [])
    task = TaskResult(
        name=event.session_name,
        command=event.command,
        exit_code=exit_code,
        duration_seconds=event.duration_seconds,
        stdout_tail=output_tail,
        stderr_tail=event.stderr_tail,
        source="claude",
    )
    detection = _event_detection(event, task, status)
    return NotificationRequest(
        task=task,
        detection=detection,
        context=NotificationContext(
            agent=AgentKind.CLAUDE,
            session_id=event.session_id,
            session_name=event.session_name,
        ),
    )


def _event_status(event: ClaudeEvent, event_name: str) -> TaskStatus:
    if event_name in _WAITING_EVENTS:
        return TaskStatus.WAITING_FOR_INPUT
    status = event.status.strip().casefold().replace("-", "_")
    if status in _SUCCESS_STATUSES:
        return TaskStatus.SUCCEEDED
    if status in _FAILED_STATUSES:
        return TaskStatus.FAILED
    raise ValueError(f"Unsupported Claude status: {event.status!r}")


def _event_detection(
    event: ClaudeEvent,
    task: TaskResult,
    status: TaskStatus,
) -> DetectionResult:
    detected = detect_output(task.combined_tail_text, task.exit_code)
    tags = dedupe_tags(["claude", event.event_name, *event.tags])
    if status is TaskStatus.WAITING_FOR_INPUT:
        return DetectionResult(
            status=status,
            tags=dedupe_tags([*tags, TaskStatus.WAITING_FOR_INPUT.value]),
            matched_phrases=detected.matched_phrases,
        )
    if detected.status is TaskStatus.WAITING_FOR_INPUT:
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=dedupe_tags([*tags, *detected.tags]),
            matched_phrases=detected.matched_phrases,
        )
    return DetectionResult(status=status, tags=dedupe_tags([*tags, status.value]))
