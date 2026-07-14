from __future__ import annotations

import asyncio
from types import SimpleNamespace

from lark_bot.codex.app_server import ServerNotification, ServerRequest
from lark_bot.codex_interactive import InteractiveSessionManager


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode or 0

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self._done.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()


class FakeGateway:
    def __init__(self, upstream_endpoint, **callbacks):
        self.upstream_endpoint = upstream_endpoint
        self.callbacks = callbacks
        self.endpoint = "ws://127.0.0.1:7777"
        self.token = "gateway-secret"
        self.started = False
        self.closed = False
        self.responses = []

    async def start(self): self.started = True
    async def close(self): self.closed = True
    async def respond_upstream(self, request_id, result=None, *, error=None):
        self.responses.append((request_id, result, error)); return True


class FakeOrchestrator:
    def __init__(self):
        self.created = []
        self.bound = []
        self.requests = []
        self.terminal = []
        self.notifications = []
        self.closed = []

    async def create_interactive_session(self, name, cwd, model=None, sandbox="workspace-write"):
        self.created.append((name, cwd, model, sandbox))
        return SimpleNamespace(id="session-1")

    def bind_interactive_thread(self, session_id, thread_id, turn_id=None):
        self.bound.append((session_id, thread_id, turn_id)); return True

    async def process_server_request(self, request, *, session_id, responder):
        self.requests.append((request, session_id, responder))

    async def resolve_terminal_request(self, request_id, result):
        self.terminal.append((request_id, result)); return True

    async def process_notification(self, notification): self.notifications.append(notification)
    async def close_interactive_session(self, session_id): self.closed.append(session_id); return True


def test_manager_starts_app_server_routes_observers_and_cleans_up():
    async def scenario():
        process = FakeProcess()
        process_calls = []
        gateways = []
        orchestrator = FakeOrchestrator()

        async def process_factory(*args, **kwargs):
            process_calls.append((args, kwargs)); return process

        def gateway_factory(endpoint, **callbacks):
            gateway = FakeGateway(endpoint, **callbacks); gateways.append(gateway); return gateway

        async def wait_listener(endpoint, proc):
            assert endpoint == "ws://127.0.0.1:6123" and proc is process

        manager = InteractiveSessionManager(
            orchestrator,
            codex_path="codex",
            process_factory=process_factory,
            gateway_factory=gateway_factory,
            which=lambda value: "C:/tools/codex.exe",
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            wait_listener=wait_listener,
        )
        await manager.start()
        descriptor = await manager.create_session(name="interactive", cwd="C:/work", model="gpt", sandbox="workspace-write")

        assert descriptor.session_id == "session-1"
        assert descriptor.endpoint == "ws://127.0.0.1:7777"
        assert descriptor.remote_auth_token == "gateway-secret"
        assert process_calls[0][0] == ("C:/tools/codex.exe", "app-server", "--listen", "ws://127.0.0.1:6123")
        gateway = gateways[0]

        await gateway.callbacks["on_terminal_request"](1, "thread/resume", {"threadId": "resume-thread"})
        assert orchestrator.bound == []
        await gateway.callbacks["on_upstream_response"](1, "thread/resume", {"threadId": "resume-thread"}, {"result": {"thread": {"id": "resume-thread"}}})
        await gateway.callbacks["on_upstream_response"](2, "thread/start", {}, {"result": {"thread": {"id": "new-thread"}}})
        request = ServerRequest("rpc-1", "item/commandExecution/requestApproval", {"threadId": "new-thread"})
        await gateway.callbacks["on_server_request"](request, gateway.respond_upstream)
        await gateway.callbacks["on_terminal_response"]("rpc-1", {"result": {"decision": "accept"}}, gateway.respond_upstream)
        notification = ServerNotification("turn/completed", {"threadId": "new-thread", "turn": {"id": "t1", "status": "completed"}})
        await gateway.callbacks["on_upstream_notification"](notification)

        assert orchestrator.bound == [("session-1", "resume-thread", None), ("session-1", "new-thread", None)]
        assert orchestrator.requests[0][0] == request
        assert orchestrator.requests[0][1] == "session-1"
        assert orchestrator.terminal == [("rpc-1", {"decision": "accept"})]
        assert orchestrator.notifications == [notification]

        assert await manager.close_session("session-1") is True
        assert gateway.closed and process.terminated
        assert orchestrator.closed == ["session-1"]
        await manager.close()

    asyncio.run(scenario())


def test_manager_does_not_bind_failed_thread_resume():
    async def scenario():
        process = FakeProcess()
        gateways = []
        orchestrator = FakeOrchestrator()

        async def process_factory(*args, **kwargs): return process
        def gateway_factory(endpoint, **callbacks):
            gateway = FakeGateway(endpoint, **callbacks); gateways.append(gateway); return gateway
        async def wait_listener(endpoint, proc): pass

        manager = InteractiveSessionManager(
            orchestrator,
            process_factory=process_factory,
            gateway_factory=gateway_factory,
            which=lambda value: "codex",
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            wait_listener=wait_listener,
        )
        await manager.start()
        await manager.create_session(name="interactive", cwd=".")
        gateway = gateways[0]

        await gateway.callbacks["on_terminal_request"](9, "thread/resume", {"threadId": "thread-failed"})
        await gateway.callbacks["on_upstream_response"](9, "thread/resume", {"threadId": "thread-failed"}, {"error": {"code": -32000, "message": "not found"}})

        assert orchestrator.bound == []
        await manager.close()

    asyncio.run(scenario())


def test_manager_rejects_non_loopback_generated_endpoint():
    async def scenario():
        manager = InteractiveSessionManager(
            FakeOrchestrator(),
            which=lambda value: "codex",
            endpoint_factory=lambda: "ws://0.0.0.0:9000",
        )
        await manager.start()
        try:
            await manager.create_session(name="x", cwd=".")
        except RuntimeError as error:
            assert "loopback" in str(error).lower()
        else:
            raise AssertionError("expected RuntimeError")

    asyncio.run(scenario())
