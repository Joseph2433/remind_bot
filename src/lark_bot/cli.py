"""Stable module alias for the command implementation."""

import sys

from lark_bot.commands import app as _implementation

sys.modules[__name__] = _implementation
