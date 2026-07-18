from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

OWNED_COMMAND = ("lark-bot", "codex-hook")
FRAGMENT_NAME = "lark-bot-notify.toml"


@dataclass(frozen=True)
class HookCheck:
    status: str
    detail: str = ""


def build_notify_override(command: Sequence[str] = OWNED_COMMAND) -> str:
    """Return a Codex `-c` override without replacing any user config."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("notify command must contain non-empty strings")
    encoded = ",".join(json.dumps(part, ensure_ascii=False) for part in command)
    return f"notify=[{encoded}]"


def _path(project: str | Path) -> Path:
    return Path(project).resolve() / ".codex" / FRAGMENT_NAME


def _fragment() -> str:
    return (
        "# Pass this file as a Codex config profile, or use the lark-bot codex launcher.\n"
        f"{build_notify_override()}\n"
    )


def install_hooks(project: str | Path) -> Path:
    """Install a non-destructive notify config fragment.

    Codex does not automatically merge arbitrary project TOML fragments.  The
    interactive launcher injects the same value with `-c`; this file is an
    auditable/manual configuration artifact and never edits config.toml.
    """

    path = _path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("refusing to replace symlink notify fragment")
    path.write_text(_fragment(), encoding="utf-8")
    return path


def uninstall_hooks(project: str | Path) -> Path:
    path = _path(project)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return path


def check_hooks(project: str | Path) -> HookCheck:
    path = _path(project)
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HookCheck("missing")
    except OSError as error:
        return HookCheck("malformed", str(error))
    if current == _fragment():
        return HookCheck("installed")
    return HookCheck("modified", "managed notify fragment differs")
