import asyncio
import queue
from types import SimpleNamespace as NS

import pytest

from lark_bot.codex_models import InteractionKind
from lark_bot.lark_control import (
    LarkControlRouter,
    LarkMessageEvent,
    LarkLongConnection,
    LarkReactionEvent,
    decode_child_event,
    normalize_message_event,
    normalize_reaction_event,
)


def run(coro):
    return asyncio.run(coro)


def test_sdk_event_normalization():
    reaction = NS(header=NS(event_id="e1"), event=NS(
        message_id="m1", reaction_type=NS(emoji_type="THUMBSUP"),
        user_id=NS(open_id="u1"),
    ))
    assert normalize_reaction_event(reaction) == LarkReactionEvent("e1", "m1", "u1", "THUMBSUP")
    message = NS(header=NS(event_id="e2"), event=NS(
        sender=NS(sender_id=NS(open_id="u2")),
        message=NS(message_type="text", content='{"text":"@_user_1 hi"}', message_id="reply",
                   parent_id="m2", root_id="root", chat_id="chat", chat_type="group", mentions=[object()]),
    ))
    normalized = normalize_message_event(message)
    assert normalized.parent_id == "m2"
    assert normalized.mentioned_bot
    with pytest.raises(ValueError):
        normalize_message_event(NS(header=NS(event_id="e"), event=NS(message=NS(message_type="image"))))


class Store:
    def __init__(self, interaction):
        self.interaction = interaction
        self.events = set()

    def record_event_once(self, event_id):
        if event_id in self.events:
            return False
        self.events.add(event_id)
        return True

    def get_pending_interaction_by_lark_message_id(self, message_id):
        return self.interaction if message_id == "prompt" else None


class Orchestrator:
    def __init__(self, question_ids=()):
        self.question_ids = question_ids
        self.calls = []

    def get_user_input_question_ids(self, interaction_id):
        return self.question_ids

    async def resolve_interaction(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return len(self.calls) == 1


@pytest.mark.parametrize("emoji, allow", [("THUMBSUP", True), ("thumbsdown", False), ("+1", True)])
def test_reaction_routes_exact_pending_approval_and_deduplicates(emoji, allow):
    interaction = NS(id="i1", kind=InteractionKind.EXEC_APPROVAL)
    store, orchestrator = Store(interaction), Orchestrator()
    router = LarkControlRouter(store, orchestrator)
    event = LarkReactionEvent("e1", "prompt", "u1", emoji)
    assert run(router.route(event)).handled
    assert orchestrator.calls == [(('i1', 'u1'), {'allow': allow})]
    assert run(router.route(event)).reason == "duplicate"


def test_group_reply_requires_mention_and_parses_multiple_answers():
    interaction = NS(id="i1", kind=InteractionKind.USER_INPUT)
    store, orchestrator = Store(interaction), Orchestrator(("q1", "q2"))
    router = LarkControlRouter(store, orchestrator)
    no_mention = LarkMessageEvent("e1", "r", "prompt", None, "c", "group", "u", "1: a\n2: b", False)
    assert run(router.route(no_mention)).reason == "bot_not_mentioned"
    mentioned = LarkMessageEvent("e2", "r", "prompt", None, "c", "group", "u", "@_user_1 q1: a\nq2: b", True)
    assert run(router.route(mentioned)).handled
    assert orchestrator.calls[-1] == (("i1", "u"), {"answers": {"q1": "a", "q2": "b"}})


def test_p2p_single_answer_and_invalid_multiple_input():
    interaction = NS(id="i1", kind=InteractionKind.USER_INPUT)
    store, orchestrator = Store(interaction), Orchestrator(("q1",))
    router = LarkControlRouter(store, orchestrator)
    event = LarkMessageEvent("e", "r", None, "prompt", "c", "p2p", "u", "answer", False)
    assert run(router.route(event)).handled
    decoded = decode_child_event({"type": "reaction", "event_id": "e", "message_id": "m", "actor_id": "u", "emoji_type": "THUMBSUP"})
    assert isinstance(decoded, LarkReactionEvent)
    with pytest.raises(ValueError):
        decode_child_event({"type": "bad"})


def test_long_connection_pumps_child_events_and_closes_idempotently():
    child_queue = queue.Queue(maxsize=2)

    class FakeProcess:
        def __init__(self, **kwargs):
            self.alive = False
            self.terminated = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True
            self.alive = False

        def join(self, timeout):
            pass

    async def scenario():
        connection = LarkLongConnection(
            "app", "secret", process_factory=FakeProcess,
            queue_factory=lambda capacity: child_queue, queue_capacity=2,
        )
        await connection.start()
        child_queue.put({"type": "reaction", "event_id": "e", "message_id": "m", "actor_id": "u", "emoji_type": "THUMBSUP"})
        event = await asyncio.wait_for(connection.events.get(), 1)
        assert event.message_id == "m"
        await connection.close()
        await connection.close()
        assert connection._process.terminated

    run(scenario())
