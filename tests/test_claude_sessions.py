import asyncio
from datetime import datetime, timezone
import pytest

from lark_bot.modules.agent.agent_model import AgentKind, AgentSession, SessionStatus
from lark_bot.modules.agent.agent_store import SQLiteAgentStore
from lark_bot.modules.claude.claude_sdk import ClaudeSdkResult
from lark_bot.modules.claude.claude_session_manager import ClaudeSessionManager


class FakeClient:
    def __init__(self, events):
        self.events = events
        self.messages = []

    async def connect(self):
        self.events.append("connect")

    async def query(self, prompt):
        self.events.append(("query", prompt))

    async def interrupt(self):
        self.events.append("interrupt")

    async def close(self):
        self.events.append("close")

    async def _messages(self):
        yield ClaudeSdkResult("provider-1", "success", False, 1, "done")

    def receive_response(self):
        return self._messages()


class PermissionClient(FakeClient):
    def __init__(self, events, callback):
        super().__init__(events)
        self.callback = callback
        self.requested = asyncio.Event()
        self.permission_result = None

    async def query(self, prompt):
        await super().query(prompt)
        self.requested.set()
        self.permission_result = await self.callback(
            "AskUserQuestion",
            {"questions": [{"question": "q-1", "options": []}], "secret": "do-not-store"},
            None,
        )


def test_cancel_wins_before_terminal_result_is_drained():
    async def scenario():
        release = asyncio.Event()
        events = []

        class Client(FakeClient):
            async def interrupt(self):
                await super().interrupt()
                release.set()
                await asyncio.sleep(0)

            async def _messages(self):
                await release.wait()
                yield ClaudeSdkResult("provider", "success", False, 1, "late")

        store = SQLiteAgentStore(":memory:")
        manager = ClaudeSessionManager(store, lambda options: Client(events), close_timeout_seconds=1)
        session = await manager.create_session("cancel", ".", "private")
        for _ in range(20):
            if manager._live[session.session_id].interactions:
                break
            await asyncio.sleep(0)
        assert await manager.cancel(session.session_id)
        await manager.wait_session(session.session_id)
        assert store.get_session(session.session_id, agent=AgentKind.CLAUDE).status is SessionStatus.CANCELLED
        await manager.close()

    asyncio.run(scenario())


def test_permission_payload_is_bounded_and_answers_stay_in_memory():
    async def scenario():
        events = []
        store = SQLiteAgentStore(":memory:")
        callback_holder = {}

        def factory(options):
            callback_holder["client"] = PermissionClient(events, options.can_use_tool)
            return callback_holder["client"]

        manager = ClaudeSessionManager(store, factory, interaction_timeout_seconds=5)
        session = await manager.create_session("input", ".", "private")
        client = callback_holder["client"]
        await asyncio.wait_for(client.requested.wait(), timeout=1)
        for _ in range(20):
            if manager._live[session.session_id].interactions:
                break
            await asyncio.sleep(0)
        interaction_id = next(iter(manager._live[session.session_id].interactions))
        interaction = store.get_interaction(interaction_id, agent=AgentKind.CLAUDE)
        assert "do-not-store" not in interaction.payload_summary
        assert manager.get_user_input_question_ids(interaction.interaction_id) == ("q-1",)
        assert await manager.resolve_interaction(interaction.interaction_id, "tester", answers={"q-1": "yes"})
        assert not await manager.resolve_interaction(interaction.interaction_id, "late", answers={"q-1": "no"})
        for _ in range(20):
            if client.permission_result is not None:
                break
            await asyncio.sleep(0)
        assert client.permission_result.allowed is True
        assert client.permission_result.updated_input == {"questions": [{"question": "q-1", "answer": "yes"}]}
        await manager.wait_session(session.session_id)
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ["connect", "query", "result"])
def test_sdk_failures_are_failed_with_safe_summary(failure_stage):
    async def scenario():
        events = []

        class Client(FakeClient):
            async def connect(self):
                if failure_stage == "connect":
                    raise RuntimeError("prompt-secret must not leak")
                await super().connect()

            async def query(self, prompt):
                if failure_stage == "query":
                    raise RuntimeError("prompt-secret must not leak")
                await super().query(prompt)

            async def _messages(self):
                if failure_stage == "result":
                    yield ClaudeSdkResult(
                        "provider",
                        "error",
                        True,
                        1,
                        "token=prompt-secret",
                    )
                    return
                async for message in super()._messages():
                    yield message

        store = SQLiteAgentStore(":memory:")
        manager = ClaudeSessionManager(store, lambda options: Client(events))
        session = await manager.create_session("failure", ".", "prompt-secret")
        await manager.wait_session(session.session_id)
        current = store.get_session(session.session_id, agent=AgentKind.CLAUDE)
        assert current.status is SessionStatus.FAILED
        assert "prompt-secret" not in current.summary
        if failure_stage == "result":
            assert "[REDACTED]" in current.summary
        await manager.close()

    asyncio.run(scenario())


def test_start_reconciles_claude_only_and_close_is_idempotent():
    async def scenario():
        now = datetime.now(timezone.utc)
        store = SQLiteAgentStore(":memory:")
        for agent, sid in ((AgentKind.CLAUDE, "claude-old"), (AgentKind.CODEX, "codex-old")):
            store.create_session(
                AgentSession(
                    session_id=sid,
                    agent=agent,
                    name=sid,
                    status=SessionStatus.RUNNING,
                    created_at=now,
                    updated_at=now,
                )
            )
        manager = ClaudeSessionManager(store, lambda options: FakeClient([]))
        await manager.start()
        await manager.start()
        assert store.get_session("claude-old", agent=AgentKind.CLAUDE).status is SessionStatus.INTERRUPTED
        assert store.get_session("codex-old", agent=AgentKind.CODEX).status is SessionStatus.RUNNING
        await manager.close()
        await manager.close()

    asyncio.run(scenario())


def test_claude_managed_session_persists_without_prompt():
    async def scenario():
        events = []
        store = SQLiteAgentStore(":memory:")
        manager = ClaudeSessionManager(store, lambda options: FakeClient(events))
        session = await manager.create_session("demo", ".", "secret prompt")
        assert events[0] == "connect"
        assert session.status is SessionStatus.RUNNING
        await manager.wait_session(session.session_id)
        current = store.get_session(session.session_id, agent=AgentKind.CLAUDE)
        assert current is not None
        assert current.status is SessionStatus.SUCCEEDED
        assert current.conversation_id == "provider-1"
        assert "secret prompt" not in repr(current)
        assert ("query", "secret prompt") in events
        await manager.close()

    asyncio.run(scenario())
