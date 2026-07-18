from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


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


class SessionRef(BaseModel):
    session_id: str = Field(min_length=1)
    agent: AgentKind
    name: str = Field(min_length=1)


class AgentSession(SessionRef):
    conversation_id: str | None = None
    status: SessionStatus
    summary: str = ""
    created_at: datetime
    updated_at: datetime


class AgentInteraction(BaseModel):
    interaction_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    status: InteractionStatus = InteractionStatus.PENDING
    lark_message_id: str | None = None


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
