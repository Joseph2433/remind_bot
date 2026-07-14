from __future__ import annotations

import re
from typing import Any

from lark_bot.codex.models import InteractionKind
from lark_bot.lark.events import (
    LarkControlEvent,
    LarkControlResult,
    LarkMessageEvent,
    LarkReactionEvent,
)


class LarkControlRouter:
    def __init__(self, store: Any, orchestrator: Any) -> None:
        self._store = store
        self._orchestrator = orchestrator

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
        if interaction.kind is InteractionKind.USER_INPUT:
            return LarkControlResult(False, "wrong_interaction_kind", interaction.id)
        emoji = event.emoji_type.casefold()
        if emoji in {"thumbsup", "+1"}:
            allow = True
        elif emoji in {"thumbsdown", "-1"}:
            allow = False
        else:
            return LarkControlResult(False, "unsupported_emoji", interaction.id)
        won = await self._orchestrator.resolve_interaction(
            interaction.id, event.actor_id, allow=allow
        )
        return LarkControlResult(
            won, "resolved" if won else "already_resolved", interaction.id
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
        if interaction.kind in {
            InteractionKind.EXEC_APPROVAL,
            InteractionKind.FILE_CHANGE_APPROVAL,
            InteractionKind.PERMISSION_REQUEST,
        }:
            allow = _parse_approval_answer(text)
            if allow is None:
                return LarkControlResult(
                    False, "invalid_approval_answer", interaction.id
                )
            won = await self._orchestrator.resolve_interaction(
                interaction.id, event.actor_id, allow=allow
            )
            return LarkControlResult(
                won, "resolved" if won else "already_resolved", interaction.id
            )
        if interaction.kind is not InteractionKind.USER_INPUT:
            return LarkControlResult(False, "wrong_interaction_kind", interaction.id)
        if event.chat_type.casefold() != "p2p" and not event.mentioned_bot:
            return LarkControlResult(False, "bot_not_mentioned", interaction.id)
        question_ids = self._orchestrator.get_user_input_question_ids(interaction.id)
        answers = _parse_answers(text, question_ids)
        if answers is None:
            return LarkControlResult(False, "invalid_answers", interaction.id)
        won = await self._orchestrator.resolve_interaction(
            interaction.id, event.actor_id, answers=answers
        )
        return LarkControlResult(
            won, "resolved" if won else "already_resolved", interaction.id
        )


_MENTION_RE = re.compile(r"(?:^|\s)@_user_\d+\b")


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub(" ", text).strip()


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
