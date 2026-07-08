from __future__ import annotations

import re

from lack_bot.models import DetectionResult, TaskStatus


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("approval", re.compile(r"\bapproval\b|do you want to allow", re.IGNORECASE)),
    ("permission", re.compile(r"\bpermission\b", re.IGNORECASE)),
    ("waiting_for_input", re.compile(r"waiting for input|need user input", re.IGNORECASE)),
]


def detect_output(output: str, exit_code: int) -> DetectionResult:
    tags: list[str] = []
    matched_phrases: list[str] = []
    for tag, pattern in _PATTERNS:
        match = pattern.search(output)
        if match:
            tags.append(tag)
            matched_phrases.append(match.group(0))

    if tags:
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=tags,
            matched_phrases=matched_phrases,
        )

    if exit_code == 0:
        return DetectionResult(status=TaskStatus.SUCCEEDED, tags=["succeeded"])
    return DetectionResult(status=TaskStatus.FAILED, tags=["failed"])
