from __future__ import annotations

import hashlib
import json

from lark_bot.core.redaction import redact_text
from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.claude.claude_model import ClaudeEvent
from lark_bot.modules.notification.notification_builder import build_agent_notification
from lark_bot.modules.notification.notification_model import AgentNotificationInput, NotificationRequest
from lark_bot.modules.task.task_model import TaskStatus

_SUPPORTED_EVENTS = frozenset(
    {
        "sessionstart",
        "notification",
        "permissionrequest",
        "userpromptsubmit",
        "stop",
        "stopfailure",
        "sessionend",
    }
)
_WAITING_NOTIFICATION_TYPES = frozenset(
    {"permission_prompt", "idle_prompt", "agent_needs_input", "elicitation_dialog"}
)
_COMPLETED_NOTIFICATION_TYPES = frozenset(
    {"auth_success", "elicitation_complete", "elicitation_response", "agent_completed"}
)
_SUMMARY_LIMIT = 512


def claude_event_to_notification(event: ClaudeEvent) -> NotificationRequest:
    original_event_name = event.hook_event_name.strip()
    event_name = original_event_name.casefold()
    if event_name not in _SUPPORTED_EVENTS:
        raise ValueError(f"Unsupported Claude event: {original_event_name!r}")

    status, semantic_tag, extra_tags = _event_semantics(event, event_name)
    safe_summary = _safe_summary(event)
    event_id = _event_id(event, original_event_name)
    return build_agent_notification(
        AgentNotificationInput(
            agent=AgentKind.CLAUDE,
            task_name=_safe_text(event.title or event.source or "claude", limit=128) or "claude",
            session_id=event.session_id,
            session_name=_safe_text(event.title or event.source or "claude", limit=128) or "claude",
            event_name=original_event_name,
            event_id=event_id,
            status=status,
            command=["claude"],
            exit_code=0 if status is not TaskStatus.FAILED else 1,
            summary=safe_summary,
            tags=[
                semantic_tag,
                *extra_tags,
                *(
                    [TaskStatus.WAITING_FOR_INPUT.value]
                    if status is TaskStatus.WAITING_FOR_INPUT
                    else []
                ),
            ],
        )
    )


def _event_semantics(event: ClaudeEvent, event_name: str) -> tuple[TaskStatus, str, list[str]]:
    if event_name == "permissionrequest":
        return TaskStatus.WAITING_FOR_INPUT, "permission_required", []
    if event_name == "stopfailure":
        return TaskStatus.FAILED, "turn_failed", []
    if event_name == "stop":
        return TaskStatus.COMPLETED, "turn_completed", []
    if event_name == "sessionstart":
        return TaskStatus.COMPLETED, "session_started", []
    if event_name == "sessionend":
        return TaskStatus.COMPLETED, "session_ended", []
    if event_name == "userpromptsubmit":
        return TaskStatus.COMPLETED, "prompt_submitted", []
    notification_type = (event.notification_type or "").strip().casefold()
    if notification_type in _WAITING_NOTIFICATION_TYPES:
        return TaskStatus.WAITING_FOR_INPUT, notification_type, [notification_type]
    if notification_type in _COMPLETED_NOTIFICATION_TYPES:
        semantic = "turn_completed" if notification_type == "agent_completed" else notification_type
        return TaskStatus.COMPLETED, semantic, [notification_type]
    raise ValueError(f"Unsupported Claude notification type: {event.notification_type!r}")


def _event_id(event: ClaudeEvent, original_event_name: str) -> str:
    prompt = event.prompt_id or "-"
    discriminator = event.notification_type or event.source or event.reason or event.error or "-"
    tool_digest = _tool_input_digest(event.tool_input)
    if event.tool_name or tool_digest:
        discriminator = f"{discriminator}|{event.tool_name or '-'}|{tool_digest}"
    payload = "|".join((event.session_id, prompt, original_event_name, discriminator))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tool_input_digest(value: object) -> str:
    if value is None:
        return "-"
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_text(value: str, *, limit: int) -> str:
    return redact_text(value).strip()[:limit]


def _safe_summary(event: ClaudeEvent) -> str:
    for value in (event.message, event.title, event.error):
        if value:
            return _safe_text(value, limit=_SUMMARY_LIMIT)
    return ""
