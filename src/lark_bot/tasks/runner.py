from __future__ import annotations

import subprocess
import time
from collections import deque
from collections.abc import Sequence

from lark_bot.models import TaskResult


def run_command(command: Sequence[str], name: str, tail_lines: int = 40) -> TaskResult:
    started = time.monotonic()
    process = subprocess.run(
        list(command),
        text=True,
        capture_output=True,
        check=False,
    )
    duration = time.monotonic() - started
    return TaskResult(
        name=name,
        command=list(command),
        exit_code=process.returncode,
        duration_seconds=duration,
        stdout_tail=_tail_lines(process.stdout, tail_lines),
        stderr_tail=_tail_lines(process.stderr, tail_lines),
    )


def _tail_lines(text: str, line_count: int) -> list[str]:
    if not text:
        return []
    return list(deque(text.splitlines(), maxlen=line_count))
