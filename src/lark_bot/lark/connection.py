"""Backward-compatible Lark connection import path."""

from lark_bot.modules.lark.lark_connection import (
    LarkLongConnection,
    decode_child_event,
)

__all__ = ["LarkLongConnection", "decode_child_event"]
