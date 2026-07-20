from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
_CONCURRENT_CHANGE_DETAIL = "settings changed during update"
_LOCK_FAILURE_DETAIL = "unable to lock settings"
_PUBLISH_FAILURE_DETAIL = "unable to publish settings"


@dataclass(frozen=True)
class HookCheck:
    status: HookStatus
    detail: str = ""


@dataclass(frozen=True)
class TargetSnapshot:
    exists: bool
    identity: tuple[int, int, int, int, int, int] | None
    digest: str | None


class _SettingsLockError(RuntimeError):
    pass


class _ConcurrentTargetChange(RuntimeError):
    pass


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


def _target_identity_from_stat(result: os.stat_result) -> tuple[int, int, int, int, int, int]:
    mtime_ns = getattr(result, "st_mtime_ns", int(result.st_mtime * 1_000_000_000))
    return (
        int(result.st_dev),
        int(result.st_ino),
        int(result.st_mode),
        int(getattr(result, "st_file_attributes", 0)),
        int(result.st_size),
        int(mtime_ns),
    )


def _target_identity(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        result = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("unable to inspect Claude Hook settings") from error
    return _target_identity_from_stat(result)


def _refuse_reparse(path: Path, identity: tuple[int, int, int, int] | None) -> None:
    if path.is_symlink() or (identity is not None and stat.S_ISLNK(identity[2])):
        raise ValueError("refusing to replace symlink Claude Hook settings")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if identity is not None and identity[3] & reparse_flag:
        raise ValueError("refusing to replace reparse point Claude Hook settings")


def _refuse_symlinks(path: Path) -> None:
    for candidate in (path.parent, path):
        _refuse_reparse(candidate, _path_identity(candidate))


@contextmanager
def _settings_lock(path: Path) -> Iterator[None]:
    lock_path = path.parent / ".lark-bot-settings.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_symlinks(path)
    _refuse_reparse(lock_path, _path_identity(lock_path))
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    acquired = False
    try:
        try:
            fd = os.open(lock_path, flags, 0o600)
            opened_stat = os.fstat(fd)
            opened_identity = (
                int(opened_stat.st_dev),
                int(opened_stat.st_ino),
                int(opened_stat.st_mode),
                int(getattr(opened_stat, "st_file_attributes", 0)),
            )
            _refuse_reparse(lock_path, opened_identity)
            if _path_identity(lock_path) != opened_identity:
                raise _SettingsLockError
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as error:
            raise _SettingsLockError from error
        acquired = True
        yield
    finally:
        if acquired and fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if fd is not None:
            os.close(fd)


def _missing_snapshot() -> TargetSnapshot:
    return TargetSnapshot(False, None, None)


def _snapshot(raw: bytes, identity: tuple[int, int, int, int, int, int]) -> TargetSnapshot:
    return TargetSnapshot(True, identity, hashlib.sha256(raw).hexdigest())


def _read(path: Path) -> tuple[dict[str, object] | None, HookCheck | None, TargetSnapshot]:
    before = _target_identity(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if before is not None:
            return None, HookCheck("modified", _CONCURRENT_CHANGE_DETAIL), _missing_snapshot()
        return {}, HookCheck("missing"), _missing_snapshot()
    except OSError:
        return None, HookCheck("malformed", "unable to read settings"), _missing_snapshot()
    after = _target_identity(path)
    if before is None or after != before:
        return None, HookCheck("modified", _CONCURRENT_CHANGE_DETAIL), _missing_snapshot()
    snapshot = _snapshot(raw, after)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, HookCheck("malformed", "settings must be valid UTF-8"), snapshot
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None, HookCheck("malformed", "settings must be valid JSON"), snapshot
    if not isinstance(value, dict):
        return None, HookCheck("malformed", "settings JSON must be an object"), snapshot
    return value, None, snapshot


def _supports_dir_fd_updates() -> bool:
    supported = getattr(os, "supports_dir_fd", set())
    return os.name != "nt" and all(
        operation in supported for operation in (os.open, os.stat, os.replace, os.unlink, os.link)
    )


def _snapshot_from_path(path: Path) -> TargetSnapshot | None:
    try:
        before = _target_identity(path)
    except ValueError:
        return None
    if before is None:
        return _missing_snapshot()
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        after = _target_identity(path)
    except ValueError:
        return None
    if after != before:
        return None
    return _snapshot(raw, after)


def _snapshot_from_dir_fd(name: str, parent_fd: int) -> TargetSnapshot | None:
    try:
        before_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _missing_snapshot()
    except OSError:
        return None
    before = _target_identity_from_stat(before_stat)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        return None
    try:
        opened = _target_identity_from_stat(os.fstat(fd))
        if opened != before:
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read()
            after_opened = _target_identity_from_stat(os.fstat(handle.fileno()))
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        after = _target_identity_from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    except OSError:
        return None
    if after != before or after_opened != before:
        return None
    return _snapshot(raw, before)


def _target_matches(path: Path, snapshot: TargetSnapshot, parent_fd: int | None) -> bool:
    current = (
        _snapshot_from_dir_fd(path.name, parent_fd)
        if parent_fd is not None
        else _snapshot_from_path(path)
    )
    return current == snapshot


def _posix_exchange(first: Path, second: Path, parent_fd: int | None) -> bool:
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        renameat2 = library.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        directory_fd = parent_fd if parent_fd is not None else -100
        first_name = os.fsencode(first.name if parent_fd is not None else first)
        second_name = os.fsencode(second.name if parent_fd is not None else second)
        return renameat2(directory_fd, first_name, directory_fd, second_name, 0x2) == 0
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        renamex_np = library.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        return renamex_np(os.fsencode(first), os.fsencode(second), 0x2) == 0
    return False


def _atomic_publish_existing(
    canonical: Path,
    replacement: Path,
    parent_fd: int | None,
    expected: TargetSnapshot,
) -> Path | None:
    if os.name == "nt":
        if not _target_matches(canonical, expected, None):
            raise _ConcurrentTargetChange
        backup = canonical.with_name(f".{canonical.name}.backup.{uuid.uuid4().hex}.bak")
        try:
            os.link(canonical, backup, follow_symlinks=False)
        except OSError:
            return None
        backup_snapshot = _snapshot_from_path(backup)
        if backup_snapshot != expected or not _target_matches(canonical, expected, None):
            _unlink_known(backup, None)
            raise _ConcurrentTargetChange
        try:
            os.replace(replacement, canonical)
        except OSError:
            _unlink_known(backup, None)
            return None
        return backup
    return replacement if _posix_exchange(canonical, replacement, parent_fd) else None


def _atomic_restore_existing(
    canonical: Path,
    backup: Path,
    parent_fd: int | None,
    published_digest: str,
) -> Path | None:
    current = (
        _snapshot_from_dir_fd(canonical.name, parent_fd)
        if parent_fd is not None
        else _snapshot_from_path(canonical)
    )
    if current is None or current.digest != published_digest:
        raise _ConcurrentTargetChange
    if os.name == "nt":
        backup_content = _read_backup(backup, None)
        if backup_content is None:
            return None
        raw, original_mode = backup_content
        recovery_fd, recovery_name = tempfile.mkstemp(
            prefix=f".{canonical.name}.recovery.",
            suffix=".tmp",
            dir=canonical.parent,
        )
        recovery = Path(recovery_name)
        try:
            with os.fdopen(recovery_fd, "wb") as handle:
                offset = 0
                while offset < len(raw):
                    written = handle.write(raw[offset:])
                    if written is None or written <= 0:
                        raise OSError("recovery write failed")
                    offset += written
                handle.flush()
                os.fsync(handle.fileno())
                try:
                    os.chmod(recovery, original_mode)
                except OSError:
                    pass
            current = _snapshot_from_path(canonical)
            if current is None or current.digest != published_digest:
                raise _ConcurrentTargetChange
            os.replace(recovery, canonical)
        except OSError:
            return None
        finally:
            _unlink_known(recovery, None)
        return backup
    return backup if _posix_exchange(canonical, backup, parent_fd) else None


def _link_no_overwrite(source: Path, destination: Path, parent_fd: int | None) -> bool:
    try:
        if parent_fd is not None:
            os.link(
                source.name,
                destination.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        else:
            os.link(source, destination, follow_symlinks=False)
    except FileExistsError:
        return False
    return True


def _unlink_known(path: Path, parent_fd: int | None) -> None:
    try:
        if parent_fd is not None:
            os.unlink(path.name, dir_fd=parent_fd)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def _probe_hardlink_support(path: Path, parent_fd: int | None) -> bool:
    probe_fd, probe_name = tempfile.mkstemp(
        prefix=f".{path.name}.link-probe.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(probe_fd)
    source = Path(probe_name)
    destination = source.with_name(f"{source.name}.linked")
    linked = False
    try:
        try:
            linked = _link_no_overwrite(source, destination, parent_fd)
        except OSError:
            return False
        return linked
    finally:
        _unlink_known(source, parent_fd)
        if linked:
            _unlink_known(destination, parent_fd)


def _read_backup(backup: Path, parent_fd: int | None) -> tuple[bytes, int] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = (
            os.open(backup.name, flags, dir_fd=parent_fd)
            if parent_fd is not None
            else os.open(backup, flags)
        )
    except OSError:
        return None
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), mode
    except OSError:
        return None
    finally:
        os.close(fd)


def _write_atomic(
    path: Path,
    value: dict[str, object],
    target_snapshot: TargetSnapshot,
) -> HookCheck | None:
    _refuse_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_symlinks(path)
    parent_identity = _parent_identity(path.parent)
    parent_fd: int | None = None
    if _supports_dir_fd_updates():
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_fd = os.open(path.parent, flags)
        try:
            opened_parent = os.fstat(parent_fd)
            opened_identity = (
                int(opened_parent.st_dev),
                int(opened_parent.st_ino),
                int(opened_parent.st_mode),
                int(getattr(opened_parent, "st_file_attributes", 0)),
            )
        except Exception:
            os.close(parent_fd)
            raise
        if opened_identity != parent_identity:
            os.close(parent_fd)
            raise ValueError("Claude Hook settings directory changed")
    temp_path: Path | None = None
    backup_path: Path | None = None
    published_existing = False
    published_digest: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            if _parent_identity(path.parent) != parent_identity:
                raise ValueError("Claude Hook settings directory changed")
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replacement_snapshot = _snapshot_from_path(temp_path)
        if replacement_snapshot is None or replacement_snapshot.digest is None:
            return HookCheck("modified", _PUBLISH_FAILURE_DETAIL)
        published_digest = replacement_snapshot.digest
        _refuse_symlinks(path)
        if _parent_identity(path.parent) != parent_identity:
            raise ValueError("Claude Hook settings directory changed")

        if target_snapshot.exists:
            if not _probe_hardlink_support(path, parent_fd):
                return HookCheck("modified", _PUBLISH_FAILURE_DETAIL)
            _refuse_symlinks(path)
            if _parent_identity(path.parent) != parent_identity:
                raise ValueError("Claude Hook settings directory changed")
            try:
                backup_path = _atomic_publish_existing(
                    path,
                    temp_path,
                    parent_fd,
                    target_snapshot,
                )
            except _ConcurrentTargetChange:
                return HookCheck("modified", _CONCURRENT_CHANGE_DETAIL)
            if backup_path is None:
                return HookCheck("modified", _PUBLISH_FAILURE_DETAIL)
            published_existing = True
            backup_snapshot = (
                _snapshot_from_dir_fd(backup_path.name, parent_fd)
                if parent_fd is not None
                else _snapshot_from_path(backup_path)
            )
            if backup_snapshot != target_snapshot:
                try:
                    displaced = _atomic_restore_existing(
                        path,
                        backup_path,
                        parent_fd,
                        published_digest,
                    )
                except _ConcurrentTargetChange:
                    _unlink_known(backup_path, parent_fd)
                    published_existing = False
                else:
                    if displaced is not None:
                        _unlink_known(displaced, parent_fd)
                        published_existing = False
                return HookCheck("modified", _CONCURRENT_CHANGE_DETAIL)
            _unlink_known(backup_path, parent_fd)
            published_existing = False
            return None

        if _parent_identity(path.parent) != parent_identity:
            raise ValueError("Claude Hook settings directory changed")
        try:
            published = _link_no_overwrite(temp_path, path, parent_fd)
        except OSError:
            return HookCheck("modified", _PUBLISH_FAILURE_DETAIL)
        if not published:
            return HookCheck("modified", _CONCURRENT_CHANGE_DETAIL)
        return None
    finally:
        if published_existing and backup_path is not None and published_digest is not None:
            try:
                displaced = _atomic_restore_existing(
                    path,
                    backup_path,
                    parent_fd,
                    published_digest,
                )
            except _ConcurrentTargetChange:
                _unlink_known(backup_path, parent_fd)
            else:
                if displaced is not None:
                    _unlink_known(displaced, parent_fd)
        if temp_path is not None:
            _unlink_known(temp_path, parent_fd)
        if parent_fd is not None:
            os.close(parent_fd)


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


def _exact_count(groups: object) -> int:
    return sum(
        handler == OWNED_HANDLER
        for group in _groups(groups)
        if isinstance(group.get("hooks"), list)
        for handler in group["hooks"]
    )


def _deduplicate_exact(groups: list[dict[str, object]]) -> list[dict[str, object]]:
    found = False
    kept_groups: list[dict[str, object]] = []
    for group in groups:
        handlers = group.get("hooks")
        assert isinstance(handlers, list)
        kept_handlers: list[object] = []
        removed_duplicate = False
        for handler in handlers:
            if handler == OWNED_HANDLER:
                if found:
                    removed_duplicate = True
                    continue
                found = True
            kept_handlers.append(handler)
        if removed_duplicate:
            if not kept_handlers:
                continue
            updated = dict(group)
            updated["hooks"] = kept_handlers
            kept_groups.append(updated)
        else:
            kept_groups.append(group)
    return kept_groups


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
                handler_type = handler.get("type")
                if not isinstance(handler_type, str) or not handler_type.strip():
                    return HookCheck("malformed", f"hooks.{event} handler type must be a nonempty string")
                if handler_type != "command":
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
    return isinstance(hooks, dict) and all(_exact_count(hooks.get(event)) == 1 for event in HOOK_EVENTS)


def check_hooks(project: str | Path) -> HookCheck:
    path = _settings_path(project)
    value, issue, _snapshot = _read(path)
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
    if any(_exact_count(hooks.get(event)) > 1 for event in HOOK_EVENTS):
        return HookCheck("modified", "duplicate managed Claude Hook handler")
    if _has_all_owned(value):
        return HookCheck("installed")
    return HookCheck("missing")


def _install_hooks_locked(path: Path) -> HookCheck:
    value, issue, target_snapshot = _read(path)
    if issue is not None and issue.status in {"malformed", "modified"}:
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
        exact_count = _exact_count(groups)
        if exact_count == 1:
            continue
        if exact_count > 1:
            assert isinstance(groups, list)
            hooks[event] = _deduplicate_exact(groups)
            changed = True
            continue
        if groups is None:
            hooks[event] = [{"hooks": [copy.deepcopy(OWNED_HANDLER)]}]
        elif isinstance(groups, list):
            groups.append({"hooks": [copy.deepcopy(OWNED_HANDLER)]})
        else:
            return HookCheck("malformed", f"hooks.{event} must be an array")
        changed = True
    if changed:
        write_issue = _write_atomic(path, value, target_snapshot)
        if write_issue is not None:
            return write_issue
    return HookCheck("installed")


def install_hooks(project: str | Path) -> HookCheck:
    path = _settings_path(project)
    _refuse_symlinks(path)
    try:
        with _settings_lock(path):
            return _install_hooks_locked(path)
    except _SettingsLockError:
        return HookCheck("modified", _LOCK_FAILURE_DETAIL)


def _uninstall_hooks_locked(path: Path) -> HookCheck:
    value, issue, target_snapshot = _read(path)
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
        write_issue = _write_atomic(path, value, target_snapshot)
        if write_issue is not None:
            return write_issue
    return check_hooks(path.parent.parent)


def uninstall_hooks(project: str | Path) -> HookCheck:
    path = _settings_path(project)
    _refuse_symlinks(path)
    if not path.parent.exists():
        return HookCheck("missing")
    try:
        with _settings_lock(path):
            return _uninstall_hooks_locked(path)
    except _SettingsLockError:
        return HookCheck("modified", _LOCK_FAILURE_DETAIL)
