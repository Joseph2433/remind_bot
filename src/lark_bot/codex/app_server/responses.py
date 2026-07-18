"""Compatibility alias for the canonical Codex app-server responses."""

import sys

from lark_bot.modules.codex.app_server import app_server_response as _implementation

sys.modules[__name__] = _implementation
