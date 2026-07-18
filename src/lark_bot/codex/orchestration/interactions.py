"""Compatibility alias for canonical Codex orchestration interactions."""

import sys

from lark_bot.modules.codex.orchestration import orchestration_interaction as _implementation

sys.modules[__name__] = _implementation
