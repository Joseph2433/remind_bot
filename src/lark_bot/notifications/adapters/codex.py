"""Compatibility alias for the canonical Codex notification adapter."""

import sys

from lark_bot.modules.codex import codex_adapter as _implementation

sys.modules[__name__] = _implementation
