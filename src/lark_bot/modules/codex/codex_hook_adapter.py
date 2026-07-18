from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from lark_bot.modules.agent.agent_hook import (
    MAX_HOOK_BYTES,
    deliver_sanitized_hook,
    parse_bounded_json_object,
    read_callback_stdin,
)

MAX_CALLBACK_BYTES = MAX_HOOK_BYTES
_HOOK_EVENTS = {"SessionStart", "PermissionRequest", "Stop"}


def read_stdin_payload(argv: Sequence[str], reader: Callable[[int], str]) -> str:
    """Avoid touching inherited terminal stdin when notify supplied argv JSON."""

    return read_callback_stdin(argv, reader, max_bytes=MAX_CALLBACK_BYTES)


def _bounded_json(raw: str) -> dict[str, Any] | None:
    return parse_bounded_json_object(raw, max_bytes=MAX_CALLBACK_BYTES)


def normalize_callback(*, argv: Sequence[str], stdin: str) -> dict[str, str] | None:
    """Normalize Codex notify argv or a structured hook stdin payload.

    Prompt and assistant output fields are intentionally never copied.
    """

    candidates: list[str] = []
    if argv:
        candidates.append(argv[-1])
    if stdin:
        candidates.append(stdin)

    payload = next((value for raw in candidates if (value := _bounded_json(raw)) is not None), None)
    if payload is None:
        return None

    if payload.get("type") == "agent-turn-complete":
        turn_id = payload.get("turn-id")
        if not isinstance(turn_id, str) or not turn_id:
            return None
        safe = {
            "hook_event_name": "Stop",
            "event_id": turn_id[:200],
            "callback_type": "agent-turn-complete",
        }
        thread_id = payload.get("thread-id")
        if isinstance(thread_id, str) and thread_id:
            safe["thread_id"] = thread_id[:200]
        return safe

    event_name = next(
        (
            payload.get(key)
            for key in ("hook_event_name", "event_name", "hook_name")
            if isinstance(payload.get(key), str)
        ),
        None,
    )
    if event_name not in _HOOK_EVENTS:
        return None
    safe = {"hook_event_name": event_name}
    event_id = payload.get("event_id")
    if isinstance(event_id, str) and event_id:
        safe["event_id"] = event_id[:200]
    return safe


def handle_callback(
    *,
    argv: Sequence[str],
    stdin: str,
    sender: Callable[[dict[str, str]], object],
    spool_dir: Path,
) -> bool:
    safe = normalize_callback(argv=argv, stdin=stdin)
    if safe is None:
        return False
    return deliver_sanitized_hook(safe, sender, spool_dir)


def forward_existing_notify(
    *,
    argv: Sequence[str],
    stdin: str,
    environ: dict[str, str] | None = None,
) -> bool:
    """Continue an existing Codex notify command without blocking the TUI."""

    source = os.environ if environ is None else environ
    if source.get("LARK_BOT_CODEX_NOTIFY_CHAIN_ACTIVE") == "1":
        return False
    raw_chain = source.get("LARK_BOT_CODEX_NOTIFY_CHAIN")
    if not raw_chain:
        return False
    try:
        chain = json.loads(raw_chain)
    except json.JSONDecodeError:
        return False
    if not isinstance(chain, list) or not chain or any(not isinstance(part, str) or not part for part in chain):
        return False

    raw_payload = argv[-1] if argv and _bounded_json(argv[-1]) is not None else stdin
    if not raw_payload or _bounded_json(raw_payload) is None:
        return False
    child_environment = dict(source)
    child_environment["LARK_BOT_CODEX_NOTIFY_CHAIN_ACTIVE"] = "1"
    child_environment.pop("LARK_BOT_CODEX_NOTIFY_CHAIN", None)
    try:
        subprocess.Popen(
            [*chain, raw_payload],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_environment,
            close_fds=True,
        )
    except OSError:
        return False
    return True
