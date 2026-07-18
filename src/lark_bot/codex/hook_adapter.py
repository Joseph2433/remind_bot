"""Compatibility alias for the canonical Codex hook adapter module."""

import sys

from lark_bot.modules.codex import codex_hook_adapter as _implementation

sys.modules[__name__] = _implementation
