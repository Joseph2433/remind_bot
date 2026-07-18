from __future__ import annotations

import shutil
import subprocess
import sys
import json
import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lark_bot.modules.codex.codex_hook import build_notify_override


ProcessRunner = Callable[..., subprocess.CompletedProcess[object]]


@dataclass(frozen=True)
class CodexTuiOptions:
    args: list[str] = field(default_factory=list)
    codex_path: str = "codex"
    callback_command: list[str] = field(
        default_factory=lambda: [sys.executable, "-m", "lark_bot", "codex-hook"]
    )
    config_path: Path | None = None
    remote_endpoint: str | None = None
    remote_auth_token: str | None = field(default=None, repr=False)


class CodexTuiLauncher:
    """Launch Codex directly so its native TUI retains the current console."""

    def __init__(self, *, process_runner: ProcessRunner = subprocess.run) -> None:
        self._process_runner = process_runner

    def run(self, options: CodexTuiOptions) -> int:
        executable = shutil.which(options.codex_path)
        if executable is None:
            raise FileNotFoundError(f"Codex executable not found: {options.codex_path}")

        if (options.remote_endpoint is None) != (options.remote_auth_token is None):
            raise ValueError("remote endpoint and token must be provided together")

        command: list[str] = [executable]
        environment: dict[str, str] | None = None
        if options.remote_endpoint is not None:
            command.extend(
                [
                    "--remote",
                    options.remote_endpoint,
                    "--remote-auth-token-env",
                    "LARK_BOT_CODEX_REMOTE_TOKEN",
                ]
            )
            environment = os.environ.copy()
            environment["LARK_BOT_CODEX_REMOTE_TOKEN"] = options.remote_auth_token or ""
        elif options.callback_command:
            command.extend(["-c", build_notify_override(options.callback_command)])
        command.extend(options.args)

        # Do not pass stdin/stdout/stderr: subprocess inherits the live console.
        existing_notify = (
            None
            if options.remote_endpoint is not None
            else _read_existing_notify(options.config_path)
        )
        if existing_notify:
            environment = environment or os.environ.copy()
            environment["LARK_BOT_CODEX_NOTIFY_CHAIN"] = json.dumps(existing_notify)
        result = (
            self._process_runner(command, env=environment)
            if environment is not None
            else self._process_runner(command)
        )
        return int(result.returncode)


def _read_existing_notify(config_path: Path | None) -> list[str] | None:
    path = config_path
    if path is None:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        path = codex_home / "config.toml"
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream).get("notify")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(part, str) or not part for part in value):
        return None
    return value
