from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OWNED_COMMAND = "lark-bot codex-hook"
_SPECS = {
    "SessionStart": {"matcher": "startup|resume|clear"},
    "PermissionRequest": {},
    "Stop": {},
}


@dataclass(frozen=True)
class HookCheck:
    status: str
    detail: str = ""


def _path(project: str | Path) -> Path:
    return Path(project).resolve() / ".codex" / "hooks.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("hooks.json must contain an object")
    return value


def _owned_entry(event: str) -> dict[str, Any]:
    entry = {**_SPECS[event], "hooks": [{"type": "command", "command": OWNED_COMMAND, "async": True}]}
    return entry


def _is_owned(entry: object) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return False
    return any(isinstance(hook, dict) and hook.get("command") == OWNED_COMMAND for hook in entry["hooks"])


def _is_exact_owned(event: str, entry: object) -> bool:
    return entry == _owned_entry(event)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("refusing to replace symlink hooks.json")
    fd, temporary = tempfile.mkstemp(prefix="hooks.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def install_hooks(project: str | Path) -> Path:
    path = _path(project)
    data = _load(path)
    for event in _SPECS:
        entries = data.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event} hooks must be a list")
        repaired_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                repaired_entries.append(entry)
                continue
            remaining_hooks = [
                hook
                for hook in entry["hooks"]
                if not (isinstance(hook, dict) and hook.get("command") == OWNED_COMMAND)
            ]
            if remaining_hooks:
                repaired_entries.append({**entry, "hooks": remaining_hooks})
        repaired_entries.append(_owned_entry(event))
        data[event] = repaired_entries
    _atomic_write(path, data)
    return path


def uninstall_hooks(project: str | Path) -> Path:
    path = _path(project)
    data = _load(path)
    for event in _SPECS:
        entries = data.get(event)
        if not isinstance(entries, list):
            continue
        kept_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept_entries.append(entry)
                continue
            remaining_hooks = [
                hook
                for hook in entry["hooks"]
                if not (isinstance(hook, dict) and hook.get("command") == OWNED_COMMAND)
            ]
            if remaining_hooks:
                kept_entries.append({**entry, "hooks": remaining_hooks})
        if kept_entries:
            data[event] = kept_entries
        else:
            data.pop(event, None)
    _atomic_write(path, data)
    return path


def check_hooks(project: str | Path) -> HookCheck:
    try:
        data = _load(_path(project))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return HookCheck("malformed", str(error))
    found = []
    for event in _SPECS:
        entries = data.get(event, [])
        exact = isinstance(entries, list) and any(_is_exact_owned(event, entry) for entry in entries)
        owned = isinstance(entries, list) and any(_is_owned(entry) for entry in entries)
        found.append((exact, owned))
    if all(exact for exact, _ in found): return HookCheck("installed")
    if any(exact or owned for exact, owned in found): return HookCheck("modified", "managed hooks differ or are missing")
    return HookCheck("missing")
