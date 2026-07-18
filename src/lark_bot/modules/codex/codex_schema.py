"""Compatibility exports for the shared agent schema."""

from lark_bot.modules.agent.agent_schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    initialize_schema,
)

__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "initialize_schema"]
