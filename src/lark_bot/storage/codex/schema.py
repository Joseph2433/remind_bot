"""Compatibility alias for the canonical Codex schema module."""

import sys

from lark_bot.modules.codex import codex_schema as _implementation

sys.modules[__name__] = _implementation
