from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from lark_bot.codex.models import SessionStatus
from lark_bot.redaction import redact_text


SUMMARY_LIMIT = 2000


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def safe_summary(value: str) -> str:
    return redact_text(value)[:SUMMARY_LIMIT]


def request_summary(params: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in ("reason", "command", "path"):
        value = params.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values.extend(str(item) for item in value if isinstance(item, str))
    questions = params.get("questions")
    if isinstance(questions, Sequence) and not isinstance(questions, (str, bytes)):
        for question in questions:
            if not isinstance(question, Mapping):
                continue
            for key in ("question", "header", "prompt"):
                value = question.get(key)
                if isinstance(value, str):
                    values.append(value)
    return safe_summary(" | ".join(values))


def terminal_status(value: object) -> SessionStatus | None:
    return {
        "completed": SessionStatus.SUCCEEDED,
        "failed": SessionStatus.FAILED,
        "interrupted": SessionStatus.INTERRUPTED,
    }.get(value)


def turn_summary(params: Mapping[str, Any], turn: Mapping[str, Any]) -> str:
    for container in (turn, params):
        error = container.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return safe_summary(error["message"])
    for container in (turn, params):
        for key in ("finalResponse", "final_response", "text", "outputText"):
            value = container.get(key)
            if isinstance(value, str):
                return safe_summary(value)
    return ""
