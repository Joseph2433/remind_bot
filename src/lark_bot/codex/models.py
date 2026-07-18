"""Compatibility alias for the canonical Codex model module."""

import sys

from lark_bot.modules.codex import codex_model as _implementation

sys.modules[__name__] = _implementation
