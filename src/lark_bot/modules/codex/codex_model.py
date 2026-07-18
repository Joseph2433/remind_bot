from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from lark_bot.modules.agent.agent_model import AgentKind, InteractionKind


class SessionStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class InteractionStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class InteractionDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    ACCEPT = "accept"
    DECLINE = "decline"
    GRANTED = "granted"
    SUBMITTED = "submitted"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CodexSession(BaseModel):
    id: str
    thread_id: str | None = None
    turn_id: str | None = None
    name: str
    cwd: str
    model: str | None = None
    sandbox: str
    status: SessionStatus
    summary: str = ""
    created_at: datetime
    updated_at: datetime

    _normalize_timestamps = field_validator("created_at", "updated_at")(_as_utc)


class PendingInteraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    session_id: str
    request_id: str
    kind: InteractionKind
    status: InteractionStatus = InteractionStatus.PENDING
    lark_message_id: str | None = None
    payload_summary: str = ""
    requested_at: datetime
    resolved_at: datetime | None = None
    expires_at: datetime
    actor_id: str | None = None
    decision: InteractionDecision | None = None

    _normalize_timestamps = field_validator(
        "requested_at",
        "resolved_at",
        "expires_at",
    )(_as_utc)

    @model_validator(mode="after")
    def _pending_has_no_resolution_metadata(self) -> PendingInteraction:
        if self.status is InteractionStatus.PENDING and any(
            value is not None
            for value in (self.resolved_at, self.actor_id, self.decision)
        ):
            raise ValueError("pending interaction cannot contain resolution metadata")
        return self


class NotificationOutboxItem(BaseModel):
    id: int
    session_id: str | None = None
    agent: AgentKind | None = None
    session_name: str | None = None
    interaction_id: str | None = None
    notification_type: str
    payload_summary: str
    attempt_count: int = 0
    next_attempt_at: datetime
    sent_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime

    _normalize_timestamps = field_validator(
        "next_attempt_at",
        "sent_at",
        "created_at",
    )(_as_utc)


class CodexAuditEntry(BaseModel):
    id: int
    session_id: str | None = None
    interaction_id: str | None = None
    event_type: str
    actor_id: str | None = None
    detail_summary: str = ""
    created_at: datetime

    _normalize_timestamps = field_validator("created_at")(_as_utc)


class StartupReconciliationResult(BaseModel):
    session_ids: list[str]
    interaction_ids: list[str]
