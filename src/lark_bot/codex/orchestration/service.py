"""Compatibility alias for canonical Codex orchestration service."""

import sys

from lark_bot.modules.codex import codex_orchestrator as _implementation

sys.modules[__name__] = _implementation
