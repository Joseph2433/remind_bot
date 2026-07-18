"""Compatibility alias for canonical Codex orchestration summaries."""

import sys

from lark_bot.modules.codex.orchestration import orchestration_summary as _implementation

sys.modules[__name__] = _implementation
