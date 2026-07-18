"""Compatibility alias for the canonical Codex store module."""

import sys

from lark_bot.modules.codex import codex_store as _implementation

sys.modules[__name__] = _implementation
