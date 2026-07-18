from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class ClaudeEvent(BaseModel):
    session_id: str = Field(min_length=1)
    session_name: str = Field(default="claude", min_length=1)
    event_name: str = Field(min_length=1)
    status: str = "completed"
    command: list[str] = Field(default_factory=lambda: ["claude"])
    exit_code: int | None = None
    duration_seconds: float = 0
    summary: str = ""
    output_tail: list[str] = Field(default_factory=list)
    stderr_tail: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_hook_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        aliases = {
            "session_id": ("sessionId",),
            "session_name": ("name",),
            "event_name": ("hook_event_name",),
            "output_tail": ("stdout_tail",),
        }
        for target, sources in aliases.items():
            if target in normalized:
                continue
            for source in sources:
                if source in normalized:
                    normalized[target] = normalized[source]
                    break
        return normalized
