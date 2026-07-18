from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel, Field, ValidationError

from lark_bot.modules.agent.agent_model import SessionRef, SessionStatus

EventModel = TypeVar("EventModel", bound=BaseModel)


class AgentEvent(BaseModel):
    session: SessionRef
    event_type: str = Field(min_length=1)
    status: SessionStatus
    summary: str = ""
    interaction_id: str | None = None


def parse_event_payload(
    payload: str,
    model: type[EventModel],
    *,
    provider: str,
) -> EventModel:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"{provider} event payload must be valid JSON.") from error
    if not isinstance(raw, dict):
        raise ValueError(f"{provider} event payload must be a JSON object.")
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise ValueError(f"Invalid {provider} event payload: {error}") from error
