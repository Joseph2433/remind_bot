from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from lack_bot.detector import detect_output
from lack_bot.models import DetectionResult, NotificationRequest, TaskResult, TaskStatus

router = APIRouter()


class AgentEvent(BaseModel):
    name: str
    status: Literal["succeeded", "failed", "waiting_for_input"]
    source: str = "agent_event"
    command: list[str] = Field(default_factory=list)
    exit_code: int = 0
    duration_seconds: float = 0
    stdout_tail: list[str] = Field(default_factory=list)
    stderr_tail: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


@router.post("/agent/events")
async def agent_events(event: AgentEvent, request: Request) -> dict[str, object]:
    notification = event_to_notification(event)
    store = getattr(request.app.state, "notification_store", None)
    notifier = getattr(request.app.state, "notifier", None)
    cooldown_seconds = int(getattr(request.app.state, "cooldown_seconds", 300))

    if store is not None and not store.should_send(notification.dedupe_key, cooldown_seconds):
        return {"ok": True, "sent": False, "reason": "cooldown"}

    if notifier is None:
        return {"ok": True, "sent": False, "reason": "notifier_not_configured"}

    notifier.send(notification)
    if store is not None:
        store.record_sent(notification.dedupe_key, notification.detection.status.value)
    return {"ok": True, "sent": True, "status": notification.detection.status.value}


def event_to_notification(event: AgentEvent) -> NotificationRequest:
    task = TaskResult(
        name=event.name,
        command=event.command or [event.source, event.name],
        exit_code=event.exit_code,
        duration_seconds=event.duration_seconds,
        stdout_tail=event.stdout_tail,
        stderr_tail=event.stderr_tail,
        source=event.source,
    )
    detection = _event_detection(event, task)
    return NotificationRequest(task=task, detection=detection)


def _event_detection(event: AgentEvent, task: TaskResult) -> DetectionResult:
    detected = detect_output(task.combined_tail_text, task.exit_code)
    if detected.status is TaskStatus.WAITING_FOR_INPUT:
        tags = [*event.tags, *detected.tags]
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=_dedupe(tags),
            matched_phrases=detected.matched_phrases,
        )

    status = TaskStatus(event.status)
    tags = event.tags or [status.value]
    return DetectionResult(status=status, tags=_dedupe(tags))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
