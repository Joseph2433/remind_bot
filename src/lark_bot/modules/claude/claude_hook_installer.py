from __future__ import annotations

import copy
import json
import math
import os
import stat
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


def _path_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        result = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("unable to inspect Claude Hook settings") from error
    return (
        int(result.st_dev),
        int(result.st_ino),
        int(result.st_mode),
        int(getattr(result, "st_file_attributes", 0)),
    )


def _parent_identity(path: Path) -> tuple[int, int, int, int]:
    identity = _path_identity(path)
    if identity is None:
        raise ValueError("Claude Hook settings directory changed")
    return identity


def _refuse_reparse(path: Path, identity: tuple[int, int, int, int] | None) -> None:
    if path.is_symlink() or (identity is not None and stat.S_ISLNK(identity[2])):
        raise ValueError("refusing to replace symlink Claude Hook settings")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if identity is not None and identity[3] & reparse_flag:
        raise ValueError("refusing to replace reparse point Claude Hook settings")


def _refuse_symlinks(path: Path) -> None:
    for candidate in (path.parent, path):
        _refuse_reparse(candidate, _path_identity(candidate))


def _read(path: Path) -> tuple[dict[str, object] | None, HookCheck | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, HookCheck("missing")
    except UnicodeDecodeError:
        return None, HookCheck("malformed", "settings must be valid UTF-8")
    except OSError:
        return None, HookCheck("malformed", "unable to read settings")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None, HookCheck("malformed", "settings must be valid JSON")
    if not isinstance(value, dict):
        return None, HookCheck("malformed", "settings JSON must be an object")
    return value, None


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    _refuse_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_symlinks(path)
    parent_identity = _parent_identity(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            if _parent_identity(path.parent) != parent_identity:
                raise ValueError("Claude Hook settings directory changed")
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _refuse_symlinks(path)
        if _parent_identity(path.parent) != parent_identity:
            raise ValueError("Claude Hook settings directory changed")
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
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                return HookCheck("malformed", f"hooks.{event} matcher group hooks must be an array")
            for handler in handlers:
                if not isinstance(handler, dict):
                    return HookCheck("malformed", f"hooks.{event} handlers must be objects")
                if handler.get("type") != "command":
                    continue
                command = handler.get("command")
                if not isinstance(command, str) or not command.strip():
                    return HookCheck("malformed", f"hooks.{event} command must be a nonempty string")
                args = handler.get("args")
                if "args" in handler and (
                    not isinstance(args, list)
                    or any(not isinstance(arg, str) or not arg.strip() for arg in args)
                ):
                    return HookCheck("malformed", f"hooks.{event} args must be nonempty strings")
                if "async" in handler and not isinstance(handler.get("async"), bool):
                    return HookCheck("malformed", f"hooks.{event} async must be boolean")
                timeout = handler.get("timeout")
                if "timeout" in handler and (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout)
                    or timeout <= 0
                ):
                    return HookCheck("malformed", f"hooks.{event} timeout must be positive")
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
