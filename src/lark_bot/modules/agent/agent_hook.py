from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

MAX_HOOK_BYTES = 64 * 1024


def parse_bounded_json_object(
    raw: str,
    *,
    max_bytes: int = MAX_HOOK_BYTES,
) -> dict[str, Any] | None:
    try:
        if len(raw.encode("utf-8")) > max_bytes:
            return None
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_callback_stdin(
    argv: Sequence[str],
    reader: Callable[[int], str],
    *,
    max_bytes: int = MAX_HOOK_BYTES,
) -> str:
    if argv and parse_bounded_json_object(argv[-1], max_bytes=max_bytes) is not None:
        return ""
    return reader(max_bytes + 1)


def deliver_sanitized_hook(
    payload: Mapping[str, str],
    sender: Callable[[dict[str, str]], object],
    spool_dir: Path,
) -> bool:
    safe = dict(payload)
    try:
        sender(safe)
        return True
    except Exception:
        try:
            spool_dir.mkdir(parents=True, exist_ok=True)
            path = spool_dir / f"hook-{uuid.uuid4().hex}.json"
            path.write_text(json.dumps(safe, ensure_ascii=False), encoding="utf-8")
            return True
        except OSError:
            return False
