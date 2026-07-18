from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class AgentKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class SessionStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
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


class InteractionKind(StrEnum):
    EXEC_APPROVAL = "exec_approval"
    FILE_CHANGE_APPROVAL = "file_change_approval"
    PERMISSION_REQUEST = "permission_request"
    USER_INPUT = "user_input"


class InteractionDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    ACCEPT = "accept"
    DECLINE = "decline"
    GRANTED = "granted"
    SUBMITTED = "submitted"


class SessionRef(BaseModel):
    session_id: str = Field(min_length=1)
    agent: AgentKind
    name: str = Field(min_length=1)


class AgentSession(SessionRef):
    conversation_id: str | None = None
    turn_id: str | None = None
    cwd: str = ""
    model: str | None = None
    sandbox: str = "workspace-write"
    permission_mode: str | None = None
    status: SessionStatus = SessionStatus.STARTING
    summary: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class AgentInteraction(BaseModel):
    interaction_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    kind: InteractionKind
    status: InteractionStatus = InteractionStatus.PENDING
    lark_message_id: str | None = None
    payload_summary: str = ""
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    expires_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor_id: str | None = None
    decision: InteractionDecision | None = None

    @field_validator("requested_at", "resolved_at", "expires_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _pending_has_no_resolution_metadata(self) -> "AgentInteraction":
        if self.status is InteractionStatus.PENDING and any((self.resolved_at, self.actor_id, self.decision)):
            raise ValueError("pending interaction cannot contain resolution metadata")
        return self


class AgentNotification(BaseModel):
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


class AgentAuditEntry(BaseModel):
    id: int
    session_id: str | None = None
    interaction_id: str | None = None
    event_type: str
    actor_id: str | None = None
    detail_summary: str = ""
    created_at: datetime


class StartupReconciliationResult(BaseModel):
    session_ids: list[str]
    interaction_ids: list[str]


class SessionDisplay(BaseModel):
    session_id: str = Field(min_length=1)
    session_name: str = Field(min_length=1)
    agent: AgentKind

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def label(self) -> str:
        return f"{self.agent.value} / {self.session_name} [{self.short_id}]"
