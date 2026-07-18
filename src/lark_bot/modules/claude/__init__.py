"""Claude Code event and Hook integration."""

from lark_bot.modules.claude.claude_adapter import (
    ClaudeEvent,
    claude_event_to_notification,
)
from lark_bot.modules.claude.claude_service import (
    ClaudeService,
    build_claude_notification_from_json,
)
from lark_bot.modules.claude.claude_hook_adapter import handle_callback, normalize_callback
from lark_bot.modules.claude.claude_hook_installer import (
    HookCheck,
    check_hooks,
    install_hooks,
    uninstall_hooks,
)

__all__ = [
    "ClaudeEvent",
    "ClaudeService",
    "build_claude_notification_from_json",
    "claude_event_to_notification",
    "HookCheck",
    "check_hooks",
    "install_hooks",
    "uninstall_hooks",
    "normalize_callback",
    "handle_callback",
]
