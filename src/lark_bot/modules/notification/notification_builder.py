from __future__ import annotations

from lark_bot.modules.notification.notification_model import (
    AgentNotificationInput,
    NotificationContext,
    NotificationRequest,
)
from lark_bot.modules.task.task_detector import dedupe_tags, detect_output
from lark_bot.modules.task.task_model import DetectionResult, TaskResult, TaskStatus


def build_agent_notification(value: AgentNotificationInput) -> NotificationRequest:
    status = value.status
    exit_code = value.exit_code
    if exit_code is None:
        exit_code = 1 if status is TaskStatus.FAILED else 0
    if status is TaskStatus.SUCCEEDED and exit_code != 0:
        status = TaskStatus.FAILED

    output_tail = value.output_tail or ([value.summary] if value.summary else [])
    task = TaskResult(
        name=value.task_name,
        command=value.command,
        exit_code=exit_code,
        duration_seconds=value.duration_seconds,
        stdout_tail=output_tail,
        stderr_tail=value.stderr_tail,
        source=value.agent.value,
    )
    detection = _build_detection(value, task, status)

    context = None
    if value.session_id:
        context = NotificationContext(
            agent=value.agent,
            session_id=value.session_id,
            session_name=value.session_name or value.task_name,
        )
    return NotificationRequest(
        task=task,
        detection=detection,
        context=context,
        event_id=value.event_id,
    )


def _build_detection(
    value: AgentNotificationInput,
    task: TaskResult,
    status: TaskStatus,
) -> DetectionResult:
    detected = detect_output(task.combined_tail_text, task.exit_code)
    event_tags = [value.event_name] if value.event_name else []
    tags = dedupe_tags([value.agent.value, *event_tags, *value.tags])

    if status is TaskStatus.WAITING_FOR_INPUT:
        if TaskStatus.WAITING_FOR_INPUT.value in tags:
            waiting_tags: list[str] = []
        elif detected.status is TaskStatus.WAITING_FOR_INPUT:
            waiting_tags = detected.tags
        else:
            waiting_tags = [TaskStatus.WAITING_FOR_INPUT.value]
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=dedupe_tags([*tags, *waiting_tags]),
            matched_phrases=detected.matched_phrases,
        )
    if status is not TaskStatus.COMPLETED and detected.status is TaskStatus.WAITING_FOR_INPUT:
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=dedupe_tags([*tags, *detected.tags]),
            matched_phrases=detected.matched_phrases,
        )
    return DetectionResult(status=status, tags=dedupe_tags([*tags, status.value]))
