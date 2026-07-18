"""Compatibility alias for the canonical Codex interactive module."""

import sys

from lark_bot.modules.codex import codex_interactive as _implementation

sys.modules[__name__] = _implementation
