import asyncio

from lark_bot.modules.agent.agent_model import AgentKind
from lark_bot.modules.agent.agent_service import AgentRegistry, AgentSessionService


def test_two_sessions_run_concurrently_and_keep_identity() -> None:
    async def scenario() -> None:
        service = AgentSessionService()
        started = {"one": asyncio.Event(), "two": asyncio.Event()}
        release = asyncio.Event()

        async def operation(session_id: str) -> str:
            started[session_id].set()
            await release.wait()
            return session_id

        first = asyncio.create_task(service.run_serialized("one", operation, "one"))
        second = asyncio.create_task(service.run_serialized("two", operation, "two"))
        await asyncio.wait_for(
            asyncio.gather(started["one"].wait(), started["two"].wait()),
            timeout=1,
        )
        release.set()

        assert await asyncio.gather(first, second) == ["one", "two"]

    asyncio.run(scenario())


def test_same_session_operations_are_serialized() -> None:
    async def scenario() -> None:
        service = AgentSessionService()
        order: list[str] = []
        first_release = asyncio.Event()

        async def first() -> None:
            order.append("first-start")
            await first_release.wait()
            order.append("first-end")

        async def second() -> None:
            order.append("second")

        first_task = asyncio.create_task(service.run_serialized("same", first))
        await asyncio.sleep(0)
        second_task = asyncio.create_task(service.run_serialized("same", second))
        await asyncio.sleep(0)
        assert order == ["first-start"]
        first_release.set()
        await asyncio.gather(first_task, second_task)
        assert order == ["first-start", "first-end", "second"]

    asyncio.run(scenario())


def test_registry_keeps_one_adapter_per_agent_kind() -> None:
    class Adapter:
        def __init__(self, agent: AgentKind) -> None:
            self.agent = agent

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def create_session(self, name: str, cwd: str, prompt: str, **options: object):
            raise NotImplementedError

        async def cancel_session(self, session_id: str) -> bool:
            return False

    registry = AgentRegistry()
    codex = Adapter(AgentKind.CODEX)
    claude = Adapter(AgentKind.CLAUDE)
    registry.register(codex)
    registry.register(claude)

    assert registry.get("codex") is codex
    assert registry.get(AgentKind.CLAUDE) is claude
    assert registry.registered() == (AgentKind.CODEX, AgentKind.CLAUDE)
