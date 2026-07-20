from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


ProcessRunner = Callable[..., subprocess.CompletedProcess[object]]


@dataclass(frozen=True)
class ClaudeTuiOptions:
    args: list[str] = field(default_factory=list)
    claude_path: str = "claude"
    env: Mapping[str, str] | None = field(default=None, repr=False)
    # ``environment`` is retained as a readable alias for callers that prefer
    # the longer name; ``env`` is the subprocess convention used by tests and
    # sibling launchers.
    environment: Mapping[str, str] | None = field(default=None, repr=False)


class ClaudeTuiLauncher:
    """Launch Claude Code while preserving its native console interaction."""

    def __init__(self, *, process_runner: ProcessRunner = subprocess.run) -> None:
        self._process_runner = process_runner

    def run(self, options: ClaudeTuiOptions) -> int:
        executable = shutil.which(options.claude_path)
        if executable is None:
            raise FileNotFoundError(f"Claude executable not found: {options.claude_path}")
        command = [executable, *options.args]
        source_environment = options.environment if options.environment is not None else options.env
        environment = dict(source_environment) if source_environment is not None else None
        result = (
            self._process_runner(command, env=environment)
            if environment is not None
            else self._process_runner(command)
        )
        return int(result.returncode)


def disabled_hook_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["LARK_BOT_CLAUDE_HOOK_DISABLED"] = "1"
    return environment


__all__ = ["ClaudeTuiLauncher", "ClaudeTuiOptions", "disabled_hook_environment"]
