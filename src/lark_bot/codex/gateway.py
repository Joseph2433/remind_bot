"""Compatibility alias for the canonical Codex gateway module."""

import sys

from lark_bot.modules.codex import codex_gateway as _implementation

sys.modules[__name__] = _implementation
