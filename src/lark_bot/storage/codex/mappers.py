"""Compatibility alias for the canonical Codex mapper module."""

import sys

from lark_bot.modules.codex import codex_mapper as _implementation

sys.modules[__name__] = _implementation
