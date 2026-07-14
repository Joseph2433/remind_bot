from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lark_bot.codex.models import SessionStatus


class OrchestratorEventType(StrEnum):
    SESSION_STARTED = "session_started"
    INTERACTION_REQUESTED = "interaction_requested"
    INTERACTION_RESOLVED = "interaction_resolved"
    SESSION_COMPLETED = "session_completed"
    SESSION_INTERRUPTED = "session_interrupted"
    TURN_COMPLETED = "turn_completed"
    TURN_INTERRUPTED = "turn_interrupted"


@dataclass(frozen=True, slots=True)
class OrchestratorEvent:
    event_type: OrchestratorEventType
    session_id: str
    interaction_id: str | None
    status: SessionStatus
    summary: str
