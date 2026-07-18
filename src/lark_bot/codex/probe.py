"""Compatibility alias for the canonical Codex probe module."""

import sys

from lark_bot.modules.codex import codex_probe as _implementation

sys.modules[__name__] = _implementation
