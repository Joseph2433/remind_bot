from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from lark_bot.core.redaction import redact_text
from lark_bot.modules.agent.agent_hook import (
    MAX_HOOK_BYTES,
    deliver_sanitized_hook,
    parse_bounded_json_object,
    read_callback_stdin,
)

MAX_CALLBACK_BYTES = MAX_HOOK_BYTES
_HOOK_EVENTS = frozenset(
    {"SessionStart", "Notification", "PermissionRequest", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"}
)
_IDENTIFIER_LIMIT = 200
_TEXT_LIMIT = 200
_ENUM_LIMIT = 100
_ENUM_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def read_stdin_payload(argv: Sequence[str], reader: Callable[[int], str]) -> str:
    return read_callback_stdin(argv, reader, max_bytes=MAX_CALLBACK_BYTES)


def _payload(raw: str) -> dict[str, Any] | None:
    return parse_bounded_json_object(raw, max_bytes=MAX_CALLBACK_BYTES)


def _identifier(value: object, *, limit: int = _IDENTIFIER_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit:
        return None
    return value


def _bounded_text(value: object, *, limit: int = _TEXT_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    value = redact_text(value).strip()
    return value[:limit] if value else None


def _enum(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _ENUM_PATTERN.fullmatch(value):
        return None
    return value[:_ENUM_LIMIT]


def normalize_callback(
    *,
    argv: Sequence[str] = (),
    stdin: str = "",
) -> dict[str, str] | None:
    candidates: list[str] = []
    if argv:
        candidates.append(argv[-1])
    if stdin:
        candidates.append(stdin)
    payload = next((parsed for raw in candidates if (parsed := _payload(raw)) is not None), None)
    if payload is None:
        return None
    supplied_agent = payload.get("agent")
    if supplied_agent is not None and supplied_agent != "claude":
        return None

    session_id = _identifier(payload.get("session_id", payload.get("sessionId")))
    event_name = _identifier(
        payload.get("hook_event_name", payload.get("event_name", payload.get("hook_name"))),
        limit=100,
    )
    if session_id is None or event_name not in _HOOK_EVENTS:
        return None

    safe: dict[str, str] = {
        "agent": "claude",
        "session_id": session_id,
        "hook_event_name": event_name,
    }
    if "prompt_id" in payload:
        value = _identifier(payload.get("prompt_id"), limit=_IDENTIFIER_LIMIT)
        if value is None:
            return None
        safe["prompt_id"] = value
    for key in ("source", "reason", "notification_type", "title"):
        value = _bounded_text(payload.get(key))
        if value is not None:
            safe[key] = value
    error = _enum(payload.get("error"))
    if error is not None:
        safe["error"] = error
    if "event_id" in payload:
        event_id = _identifier(payload.get("event_id"))
        if event_id is None:
            return None
        safe["event_id"] = event_id
    else:
        # Claude supplies no event ID. Deterministic hashing would collapse
        # legitimate identical approvals; persisted UUIDs survive replay unchanged.
        safe["event_id"] = uuid.uuid4().hex
    return safe


def handle_callback(
    *,
    argv: Sequence[str] = (),
    stdin: str = "",
    sender: Callable[[dict[str, str]], object],
    spool_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if environ is None else environ
    if source.get("LARK_BOT_CLAUDE_HOOK_DISABLED") == "1":
        return False
    safe = normalize_callback(argv=argv, stdin=stdin)
    if safe is None:
        return False
    return deliver_sanitized_hook(safe, sender, spool_dir)
