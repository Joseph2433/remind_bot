from __future__ import annotations

import re
from typing import Any

from lark_bot.modules.agent.agent_model import InteractionKind
from lark_bot.modules.lark.lark_event import (
    LarkControlEvent,
    LarkControlResult,
    LarkMessageEvent,
    LarkReactionEvent,
)


class LarkControlRouter:
    def __init__(
        self,
        store: Any,
        orchestrator: Any | None = None,
        *,
        dispatcher: Any | None = None,
    ) -> None:
        self._store = store
        self._resolver = dispatcher or orchestrator

    async def route(self, event: LarkControlEvent) -> LarkControlResult:
        if not self._store.record_event_once(event.event_id):
            return LarkControlResult(False, "duplicate")
        if not event.actor_id:
            return LarkControlResult(False, "missing_actor")
        if isinstance(event, LarkReactionEvent):
            return await self._route_reaction(event)
        return await self._route_message(event)

    async def _route_reaction(self, event: LarkReactionEvent) -> LarkControlResult:
        interaction = self._store.get_pending_interaction_by_lark_message_id(
            event.message_id
        )
        if interaction is None:
            return LarkControlResult(False, "not_pending")
        if _interaction_kind_is(interaction, InteractionKind.USER_INPUT):
            return LarkControlResult(False, "wrong_interaction_kind", _interaction_id(interaction))
        emoji = event.emoji_type.casefold()
        if emoji in {"thumbsup", "+1"}:
            allow = True
        elif emoji in {"thumbsdown", "-1"}:
            allow = False
        else:
            return LarkControlResult(False, "unsupported_emoji", _interaction_id(interaction))
        won = await self._resolver.resolve_interaction(
            _interaction_id(interaction), event.actor_id, allow=allow
        )
        return LarkControlResult(
            won, "resolved" if won else "already_resolved", _interaction_id(interaction)
        )

    async def _route_message(self, event: LarkMessageEvent) -> LarkControlResult:
        correlation_id = event.parent_id or event.root_id
        if not correlation_id:
            return LarkControlResult(False, "not_a_reply")
        interaction = self._store.get_pending_interaction_by_lark_message_id(
            correlation_id
        )
        if interaction is None:
            return LarkControlResult(False, "not_pending")
        text = _strip_mentions(event.text)
        if _interaction_kind_value(interaction) in {
            InteractionKind.EXEC_APPROVAL.value,
            InteractionKind.FILE_CHANGE_APPROVAL.value,
            InteractionKind.PERMISSION_REQUEST.value,
        }:
            allow = _parse_approval_answer(text)
            if allow is None:
                return LarkControlResult(
                    False, "invalid_approval_answer", _interaction_id(interaction)
                )
            won = await self._resolver.resolve_interaction(
                _interaction_id(interaction), event.actor_id, allow=allow
            )
            return LarkControlResult(
                won, "resolved" if won else "already_resolved", _interaction_id(interaction)
            )
        if not _interaction_kind_is(interaction, InteractionKind.USER_INPUT):
            return LarkControlResult(False, "wrong_interaction_kind", _interaction_id(interaction))
        if event.chat_type.casefold() != "p2p" and not event.mentioned_bot:
            return LarkControlResult(False, "bot_not_mentioned", _interaction_id(interaction))
        question_ids = self._resolver.get_user_input_question_ids(_interaction_id(interaction))
        answers = _parse_answers(text, question_ids)
        if answers is None:
            return LarkControlResult(False, "invalid_answers", _interaction_id(interaction))
        won = await self._resolver.resolve_interaction(
            _interaction_id(interaction), event.actor_id, answers=answers
        )
        return LarkControlResult(
            won, "resolved" if won else "already_resolved", _interaction_id(interaction)
        )


_MENTION_RE = re.compile(r"(?:^|\s)@_user_\d+\b")


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub(" ", text).strip()


def _interaction_kind_value(interaction: Any) -> str | None:
    kind = getattr(interaction, "kind", None)
    value = getattr(kind, "value", kind)
    return value if isinstance(value, str) else None


def _interaction_id(interaction: Any) -> str:
    value = getattr(interaction, "interaction_id", None)
    if value is None:
        value = getattr(interaction, "id", None)
    return str(value)


def _interaction_kind_is(interaction: Any, expected: InteractionKind) -> bool:
    return _interaction_kind_value(interaction) == expected.value


def _parse_approval_answer(text: str) -> bool | None:
    answer = text.strip().casefold()
    if answer in {"yes", "y"}:
        return True
    if answer in {"no", "n"}:
        return False
    return None


def _parse_answers(text: str, question_ids: tuple[str, ...]) -> dict[str, str] | None:
    if not text or not question_ids:
        return None
    if len(question_ids) == 1:
        return {question_ids[0]: text}
    answers: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            return None
        key, answer = (part.strip() for part in line.split(":", 1))
        if key.isdigit() and 1 <= int(key) <= len(question_ids):
            question_id = question_ids[int(key) - 1]
        elif key in question_ids:
            question_id = key
        else:
            return None
        if not answer or question_id in answers:
            return None
        answers[question_id] = answer
    return answers if set(answers) == set(question_ids) else None
