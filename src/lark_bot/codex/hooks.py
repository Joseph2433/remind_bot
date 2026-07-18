"""Compatibility alias for the canonical Codex hook module."""

import sys

from lark_bot.modules.codex import codex_hook as _implementation

sys.modules[__name__] = _implementation
