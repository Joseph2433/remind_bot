from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

OWNED_HANDLER = {
    "type": "command",
    "command": "lark-bot",
    "args": ["claude-hook"],
    "async": True,
    "timeout": 10,
}
HOOK_EVENTS = (
    "SessionStart",
    "Notification",
    "PermissionRequest",
    "Stop",
    "StopFailure",
    "SessionEnd",
)
HookStatus = Literal["installed", "missing", "modified", "malformed"]


@dataclass(frozen=True)
class HookCheck:
    status: HookStatus
    detail: str = ""


def _settings_path(project: str | Path) -> Path:
    return Path(project).resolve() / ".claude" / "settings.json"


def _refuse_symlinks(path: Path) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("refusing to replace symlink Claude Hook settings")


def _read(path: Path) -> tuple[dict[str, object] | None, HookCheck | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, HookCheck("missing")
    except OSError as error:
        return None, HookCheck("malformed", str(error))
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, HookCheck("malformed", str(error))
    if not isinstance(value, dict):
        return None, HookCheck("malformed", "settings JSON must be an object")
    return value, None


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    _refuse_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _refuse_symlinks(path)
        temp_path.replace(path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _hooks(value: dict[str, object], *, create: bool) -> dict[str, object] | None:
    current = value.get("hooks")
    if current is None and create:
        current = {}
        value["hooks"] = current
    return current if isinstance(current, dict) else None


def _groups(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [group for group in value if isinstance(group, dict)]


def _contains_exact(groups: object) -> bool:
    return any(
        isinstance(group.get("hooks"), list) and OWNED_HANDLER in group["hooks"]
        for group in _groups(groups)
    )


def _contains_owned_variant(groups: object) -> bool:
    for group in _groups(groups):
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            continue
        for handler in handlers:
            if isinstance(handler, dict) and handler.get("type") == "command" and handler.get("command") == "lark-bot":
                args = handler.get("args")
                if isinstance(args, list) and args and args[0] == "claude-hook":
                    return handler != OWNED_HANDLER
    return False


def _validate_managed_hooks(value: dict[str, object]) -> HookCheck | None:
    hooks = value.get("hooks")
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        return HookCheck("malformed", "hooks must be an object")
    for event in HOOK_EVENTS:
        if event not in hooks:
            continue
        groups = hooks[event]
        if not isinstance(groups, list):
            return HookCheck("malformed", f"hooks.{event} must be an array")
        for group in groups:
            if not isinstance(group, dict):
                return HookCheck("malformed", f"hooks.{event} matcher groups must be objects")
            if not isinstance(group.get("hooks"), list):
                return HookCheck("malformed", f"hooks.{event} matcher group hooks must be an array")
    return None


def _has_all_owned(value: dict[str, object]) -> bool:
    hooks = _hooks(value, create=False)
    return isinstance(hooks, dict) and all(_contains_exact(hooks.get(event)) for event in HOOK_EVENTS)


def check_hooks(project: str | Path) -> HookCheck:
    path = _settings_path(project)
    value, issue = _read(path)
    if issue is not None and issue.status == "missing":
        return issue
    if issue is not None:
        return issue
    assert value is not None
    structure_issue = _validate_managed_hooks(value)
    if structure_issue is not None:
        return structure_issue
    hooks = _hooks(value, create=False)
    if not isinstance(hooks, dict):
        return HookCheck("missing")
    if any(_contains_owned_variant(hooks.get(event)) for event in HOOK_EVENTS):
        return HookCheck("modified", "managed Claude Hook handler differs")
    if _has_all_owned(value):
        return HookCheck("installed")
    return HookCheck("missing")


def install_hooks(project: str | Path) -> HookCheck:
    path = _settings_path(project)
    _refuse_symlinks(path)
    value, issue = _read(path)
    if issue is not None and issue.status == "malformed":
        return issue
    if value is None:
        return HookCheck("malformed", "settings JSON must be an object")
    structure_issue = _validate_managed_hooks(value)
    if structure_issue is not None:
        return structure_issue
    hooks = _hooks(value, create=True)
    if hooks is None:
        return HookCheck("malformed", "hooks must be an object")

    if any(_contains_owned_variant(hooks.get(event)) for event in HOOK_EVENTS):
        return HookCheck("modified", "managed Claude Hook handler differs")

    changed = False
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if _contains_exact(groups):
            continue
        if groups is None:
            hooks[event] = [{"hooks": [copy.deepcopy(OWNED_HANDLER)]}]
        elif isinstance(groups, list):
            groups.append({"hooks": [copy.deepcopy(OWNED_HANDLER)]})
        else:
            return HookCheck("malformed", f"hooks.{event} must be an array")
        changed = True
    if changed:
        _write_atomic(path, value)
    return HookCheck("installed")


def uninstall_hooks(project: str | Path) -> HookCheck:
    path = _settings_path(project)
    _refuse_symlinks(path)
    value, issue = _read(path)
    if issue is not None:
        return issue
    assert value is not None
    structure_issue = _validate_managed_hooks(value)
    if structure_issue is not None:
        return structure_issue
    hooks = _hooks(value, create=False)
    if not isinstance(hooks, dict):
        return HookCheck("missing")

    changed = False
    for event in HOOK_EVENTS:
        if event not in hooks:
            continue
        groups = hooks.get(event)
        assert isinstance(groups, list)
        kept_groups: list[dict[str, object]] = []
        for group in groups:
            assert isinstance(group, dict)
            handlers = group.get("hooks")
            assert isinstance(handlers, list)
            remaining = [handler for handler in handlers if handler != OWNED_HANDLER]
            if len(remaining) != len(handlers):
                changed = True
            if remaining:
                updated = dict(group)
                updated["hooks"] = remaining
                kept_groups.append(updated)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
            changed = True
    if not hooks and "hooks" in value:
        value.pop("hooks", None)
        changed = True
    if changed:
        _write_atomic(path, value)
    return check_hooks(project)
