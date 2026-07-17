from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

MessageFormat = Literal["card", "text"]
HeaderTemplate = Literal["green", "red", "orange", "blue", "purple", "wathet", "turquoise", "yellow", "grey"]


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """Outbound Lark IM payload pieces before receive_id is attached."""

    msg_type: str
    content: dict[str, Any]


def build_text_message(receive_id: str, text: str) -> dict[str, Any]:
    return {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }


def build_interactive_message(receive_id: str, card: dict[str, Any]) -> dict[str, Any]:
    return {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }


def build_api_payload(receive_id: str, message: RenderedMessage) -> dict[str, Any]:
    return {
        "receive_id": receive_id,
        "msg_type": message.msg_type,
        "content": json.dumps(message.content, ensure_ascii=False),
    }


def text_message(text: str) -> RenderedMessage:
    return RenderedMessage(msg_type="text", content={"text": text})


def interactive_card(
    *,
    title: str,
    markdown: str,
    template: HeaderTemplate = "blue",
) -> RenderedMessage:
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title[:100]},
            "template": template,
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": markdown,
                }
            ]
        },
    }
    return RenderedMessage(msg_type="interactive", content=card)
