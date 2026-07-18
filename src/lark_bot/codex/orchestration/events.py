"""Compatibility alias for canonical Codex orchestration events."""

import sys

from lark_bot.modules.codex.orchestration import orchestration_event as _implementation

sys.modules[__name__] = _implementation
