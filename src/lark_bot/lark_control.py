from __future__ import annotations

import asyncio
import json
import multiprocessing
import queue
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, TypeAlias

from lark_bot.codex_models import InteractionKind


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
        return LarkControlResult(won, "resolved" if won else "already_resolved", interaction.id)

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
        return LarkControlResult(won, "resolved" if won else "already_resolved", interaction.id)


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


def _safe_child_put(output_queue: Any, event: LarkControlEvent) -> None:
    payload = {"type": "reaction" if isinstance(event, LarkReactionEvent) else "message", **asdict(event)}
    output_queue.put_nowait(payload)


def _lark_ws_worker(app_id: str, app_secret: str, output_queue: Any) -> None:
    import lark_oapi as lark

    def on_message(event: object) -> None:
        try:
            _safe_child_put(output_queue, normalize_message_event(event))
        except (ValueError, queue.Full):
            return

    def on_reaction(event: object) -> None:
        try:
            _safe_child_put(output_queue, normalize_reaction_event(event))
        except (ValueError, queue.Full):
            return

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_im_message_reaction_created_v1(on_reaction)
        .build()
    )
    lark.ws.Client(app_id, app_secret, event_handler=handler).start()


def decode_child_event(payload: object) -> LarkControlEvent:
    if not isinstance(payload, Mapping):
        raise ValueError("malformed child event")
    kind = payload.get("type")
    cls: type[LarkReactionEvent] | type[LarkMessageEvent]
    cls = LarkReactionEvent if kind == "reaction" else LarkMessageEvent if kind == "message" else None  # type: ignore[assignment]
    if cls is None:
        raise ValueError("unknown child event type")
    try:
        return cls(**{key: value for key, value in payload.items() if key != "type"})
    except TypeError as error:
        raise ValueError("malformed child event") from error


class LarkLongConnection:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        process_factory: Callable[..., Any] | None = None,
        queue_factory: Callable[[int], Any] | None = None,
        queue_capacity: int = 100,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        context = multiprocessing.get_context("spawn")
        self._app_id = app_id
        self._app_secret = app_secret
        self._process_factory = process_factory or context.Process
        self._queue_factory = queue_factory or (lambda size: context.Queue(maxsize=size))
        self._capacity = queue_capacity
        self.events: asyncio.Queue[LarkControlEvent] = asyncio.Queue(maxsize=queue_capacity)
        self.terminal_error: BaseException | None = None
        self._child_queue: Any = None
        self._process: Any = None
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._pump_task is not None:
            return
        if self._closed:
            raise RuntimeError("LarkLongConnection is closed")
        self._child_queue = self._queue_factory(self._capacity)
        self._process = self._process_factory(
            target=_lark_ws_worker,
            args=(self._app_id, self._app_secret, self._child_queue),
            daemon=True,
        )
        self._process.start()
        self._pump_task = asyncio.create_task(self._pump(), name="lark-long-connection-pump")

    async def _pump(self) -> None:
        try:
            while True:
                try:
                    payload = await asyncio.to_thread(self._child_queue.get, True, 0.1)
                except queue.Empty:
                    if self._process is not None and not self._process.is_alive():
                        raise RuntimeError("Lark long-connection child stopped")
                    continue
                event = decode_child_event(payload)
                self.events.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self.terminal_error = error

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 1.0)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                await asyncio.to_thread(process.join, 1.0)
        if self._pump_task is not None:
            self._pump_task.cancel()
            await asyncio.gather(self._pump_task, return_exceptions=True)
        if self._child_queue is not None:
            close = getattr(self._child_queue, "close", None)
            if close is not None:
                close()

    async def __aenter__(self) -> LarkLongConnection:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()
