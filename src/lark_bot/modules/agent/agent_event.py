from __future__ import annotations

from pydantic import BaseModel, Field

from lark_bot.modules.agent.agent_model import SessionRef, SessionStatus


class AgentEvent(BaseModel):
    session: SessionRef
    event_type: str = Field(min_length=1)
    status: SessionStatus
    summary: str = ""
    interaction_id: str | None = None
