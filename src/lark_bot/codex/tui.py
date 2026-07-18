"""Compatibility alias for the canonical Codex TUI module."""

import sys

from lark_bot.modules.codex import codex_tui as _implementation

sys.modules[__name__] = _implementation
