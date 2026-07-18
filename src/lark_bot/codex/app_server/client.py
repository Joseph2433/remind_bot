"""Compatibility alias for the canonical Codex app-server client."""

import sys

from lark_bot.modules.codex.app_server import app_server_client as _implementation

sys.modules[__name__] = _implementation
