from __future__ import annotations

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.claude.claude_model import ClaudeEvent
from lark_bot.modules.notification.notification_builder import build_agent_notification
from lark_bot.modules.notification.notification_model import AgentNotificationInput, NotificationRequest
from lark_bot.modules.task.task_model import TaskStatus

_SUPPORTED_EVENTS = frozenset({"stop", "permissionrequest", "userpromptsubmit"})
_WAITING_EVENTS = frozenset({"permissionrequest", "userpromptsubmit"})
_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "complete", "done"})
_FAILED_STATUSES = frozenset({"failed", "failure", "error", "errored"})


def claude_event_to_notification(event: ClaudeEvent) -> NotificationRequest:
    event_name = event.event_name.strip().casefold()
    if event_name not in _SUPPORTED_EVENTS:
        raise ValueError(f"Unsupported Claude event: {event.event_name!r}")

    status = _event_status(event, event_name)
    tags = list(event.tags)
    if status is TaskStatus.WAITING_FOR_INPUT:
        tags.append(TaskStatus.WAITING_FOR_INPUT.value)
    return build_agent_notification(
        AgentNotificationInput(
            agent=AgentKind.CLAUDE,
            task_name=event.session_name,
            session_id=event.session_id,
            session_name=event.session_name,
            event_name=event.event_name,
            status=status,
            command=event.command,
            exit_code=event.exit_code,
            duration_seconds=event.duration_seconds,
            summary=event.summary,
            output_tail=event.output_tail,
            stderr_tail=event.stderr_tail,
            tags=tags,
        )
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
