from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING_FOR_INPUT = "waiting_for_input"


class DetectionResult(BaseModel):
    status: TaskStatus
    tags: list[str] = Field(default_factory=list)
    matched_phrases: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    name: str
    command: list[str]
    exit_code: int
    duration_seconds: float
    stdout_tail: list[str] = Field(default_factory=list)
    stderr_tail: list[str] = Field(default_factory=list)
    source: str = "wrapper"

    @property
    def combined_tail_text(self) -> str:
        parts: list[str] = []
        if self.stdout_tail:
            parts.append("stdout:")
            parts.extend(self.stdout_tail)
        if self.stderr_tail:
            parts.append("stderr:")
            parts.extend(self.stderr_tail)
        return "\n".join(parts)
