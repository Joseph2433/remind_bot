from __future__ import annotations

import re


_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?token)"
    r"(\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)([^\s,;]+)")


def redact_text(text: str) -> str:
    redacted = _KEY_VALUE_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return _BEARER_SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
