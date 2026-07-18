from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClaudeEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1, max_length=200)
    hook_event_name: str = Field(min_length=1, max_length=100)
    prompt_id: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=100)
    notification_type: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=1000)
    error: str | None = Field(default=None, max_length=100)
    stop_hook_active: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_hook_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        aliases = {
            "session_id": ("sessionId",),
            "hook_event_name": ("event_name",),
        }
        for target, sources in aliases.items():
            if target in normalized:
                continue
            for source in sources:
                if source in normalized:
                    normalized[target] = normalized[source]
                    break
        return normalized
