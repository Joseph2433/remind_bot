from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True)
class LarkReactionEvent:
    event_id: str
    message_id: str
    actor_id: str
    emoji_type: str


@dataclass(frozen=True, slots=True)
class LarkMessageEvent:
    event_id: str
    message_id: str
    parent_id: str | None
    root_id: str | None
    chat_id: str
    chat_type: str
    actor_id: str
    text: str
    mentioned_bot: bool


LarkControlEvent: TypeAlias = LarkReactionEvent | LarkMessageEvent


@dataclass(frozen=True, slots=True)
class LarkControlResult:
    handled: bool
    reason: str
    interaction_id: str | None = None


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {field}")
    return value


def _attr(value: object, name: str) -> Any:
    return getattr(value, name, None)


def _event_id(event: object) -> str:
    return _required_string(_attr(_attr(event, "header"), "event_id"), "event_id")


def _actor_id(user_id: object) -> str:
    return _required_string(
        _attr(user_id, "open_id") or _attr(user_id, "user_id"), "actor_id"
    )


def normalize_reaction_event(event: object) -> LarkReactionEvent:
    body = _attr(event, "event")
    reaction = _attr(body, "reaction_type")
    return LarkReactionEvent(
        event_id=_event_id(event),
        message_id=_required_string(_attr(body, "message_id"), "message_id"),
        actor_id=_actor_id(_attr(body, "user_id")),
        emoji_type=_required_string(_attr(reaction, "emoji_type"), "emoji_type"),
    )


def normalize_message_event(event: object) -> LarkMessageEvent:
    body = _attr(event, "event")
    message = _attr(body, "message")
    if _attr(message, "message_type") != "text":
        raise ValueError("unsupported message type")
    content = _attr(message, "content")
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError) as error:
        raise ValueError("malformed message content") from error
    if not isinstance(parsed, dict):
        raise ValueError("malformed message content")
    sender = _attr(body, "sender")
    sender_id = _attr(sender, "sender_id")
    mentions = _attr(message, "mentions")
    return LarkMessageEvent(
        event_id=_event_id(event),
        message_id=_required_string(_attr(message, "message_id"), "message_id"),
        parent_id=_attr(message, "parent_id") or None,
        root_id=_attr(message, "root_id") or None,
        chat_id=_required_string(_attr(message, "chat_id"), "chat_id"),
        chat_type=_required_string(_attr(message, "chat_type"), "chat_type"),
        actor_id=_actor_id(sender_id),
        text=_required_string(parsed.get("text"), "text"),
        mentioned_bot=bool(mentions),
    )
