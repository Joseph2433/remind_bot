from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from lark_bot import __version__
from lark_bot.codex_app_server import (
    CodexAppServerClient,
    ProcessExitedError,
    ProtocolError,
    ServerNotification,
    ServerRequest,
    ServerRpcError,
    command_approval_response,
    file_approval_response,
    permission_response,
    user_input_response,
)


class FakeReader:
    def __init__(self) -> None:
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self._lines.get()

    async def read(self, size: int = -1) -> bytes:
        return await self._lines.get()

    def feed_json(self, value: object) -> None:
        self._lines.put_nowait(json.dumps(value).encode() + b"\n")

    def feed(self, value: bytes) -> None:
        self._lines.put_nowait(value)


class FakeWriter:
    def __init__(self, on_message: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.messages: list[dict[str, Any]] = []
        self.closed = False
        self.drain_error: BaseException | None = None
        self.message_written = asyncio.Event()
        self._on_message = on_message

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        self.messages.append(message)
        self.message_written.set()
        if self._on_message is not None:
            self._on_message(message)

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class BlockingWriter(FakeWriter):
    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        *,
        block_on_drain_call: int,
    ) -> None:
        super().__init__(on_message)
        self.block_on_drain_call = block_on_drain_call
        self.drain_calls = 0
        self.drain_blocked = asyncio.Event()
        self._never = asyncio.Event()

    async def drain(self) -> None:
        self.drain_calls += 1
        if self.drain_calls == self.block_on_drain_call:
            self.drain_blocked.set()
            await self._never.wait()
        await super().drain()


class FakeProcess:
    def __init__(self, *, auto_initialize: bool = True) -> None:
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self.returncode: int | None = None
        self.terminated = 0
        self.killed = 0
        self._waited = asyncio.Event()

        def on_message(message: dict[str, Any]) -> None:
            if auto_initialize and message.get("method") == "initialize":
                self.stdout.feed_json(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "serverInfo": {"name": "codex", "version": "test"},
                            "userAgent": "codex-test",
                        },
                    }
                )

        self.stdin = FakeWriter(on_message)

    async def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        await self._waited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.exit(-15)

    def kill(self) -> None:
        self.killed += 1
        self.exit(-9)

    def exit(self, returncode: int) -> None:
        self.returncode = returncode
        self._waited.set()


class RacingExitProcess(FakeProcess):
    def terminate(self) -> None:
        self.terminated += 1
        self.exit(0)
        raise ProcessLookupError


class StubbornProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.terminate_called = asyncio.Event()

    def terminate(self) -> None:
        self.terminated += 1
        self.terminate_called.set()


class BlockingWriteProcess(StubbornProcess):
    def __init__(self, *, block_on_drain_call: int) -> None:
        super().__init__()

        def on_message(message: dict[str, Any]) -> None:
            if message.get("method") == "initialize":
                self.stdout.feed_json(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "serverInfo": {"name": "codex", "version": "test"},
                            "userAgent": "codex-test",
                        },
                    }
                )

        self.stdin = BlockingWriter(
            on_message, block_on_drain_call=block_on_drain_call
        )


class StreamProcess(FakeProcess):
    def __init__(self, limit: int) -> None:
        super().__init__(auto_initialize=False)
        self.stdout = asyncio.StreamReader(limit=limit)
        self.stderr = asyncio.StreamReader(limit=limit)

        def on_message(message: dict[str, Any]) -> None:
            if message.get("method") == "initialize":
                response = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "serverInfo": {"name": "codex", "version": "test"},
                        "userAgent": "codex-test",
                    },
                }
                self.stdout.feed_data(json.dumps(response).encode() + b"\n")

        self.stdin = FakeWriter(on_message)


class StreamFactory:
    def __init__(self) -> None:
        self.process: StreamProcess | None = None
        self.kwargs: dict[str, object] = {}

    async def __call__(self, *args: object, **kwargs: object) -> StreamProcess:
        self.kwargs = kwargs
        self.process = StreamProcess(int(kwargs["limit"]))
        return self.process


class FakeFactory:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def __call__(self, *args: object, **kwargs: object) -> FakeProcess:
        self.calls.append((args, kwargs))
        return self.process


class DelayedFactory(FakeFactory):
    def __init__(self, process: FakeProcess) -> None:
        super().__init__(process)
        self.called = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, *args: object, **kwargs: object) -> FakeProcess:
        self.calls.append((args, kwargs))
        self.called.set()
        await self.release.wait()
        return self.process


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def wait_for_message_count(writer: FakeWriter, count: int) -> None:
    async def wait_until_ready() -> None:
        while len(writer.messages) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_ready(), 0.1)


def test_start_launches_stdio_server_and_initializes_once() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        factory = FakeFactory(process)
        client = CodexAppServerClient(process_factory=factory, codex_path="custom-codex")

        await client.start()
        await client.start()

        assert len(factory.calls) == 1
        args, kwargs = factory.calls[0]
        assert args == ("custom-codex", "app-server", "--listen", "stdio://")
        assert set(kwargs) == {"stdin", "stdout", "stderr", "limit"}
        assert kwargs["limit"] > 1024 * 1024
        assert process.stdin.messages == [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "lark-bot",
                        "title": "Lark Bot",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"jsonrpc": "2.0", "method": "initialized"},
        ]
        await client.close()

    run(scenario())


def test_close_racing_delayed_factory_cleans_eventual_process() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        factory = DelayedFactory(process)
        client = CodexAppServerClient(process_factory=factory)

        start_task = asyncio.create_task(client.start())
        await factory.called.wait()
        close_task = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        assert close_task.done() is False

        factory.release.set()
        with pytest.raises(RuntimeError, match="closed"):
            await start_task
        await close_task

        assert process.terminated == 1
        assert process.stdin.closed is True
        assert client.is_running is False
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("codex-app-server-")
            and not task.done()
        ]

    run(scenario())


def test_cancelled_close_does_not_cancel_shared_cleanup() -> None:
    async def scenario() -> None:
        process = StubbornProcess()
        client = CodexAppServerClient(
            process_factory=FakeFactory(process), close_timeout=0.001
        )
        await client.start()

        first_close = asyncio.create_task(client.close())
        await process.terminate_called.wait()
        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close

        await client.close()
        await client.wait_closed()
        assert process.killed == 1
        assert process.stdin.closed is True
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("codex-app-server-")
            and not task.done()
        ]

    run(scenario())


def test_close_bounds_initialize_write_with_forever_blocked_drain() -> None:
    async def scenario() -> None:
        process = BlockingWriteProcess(block_on_drain_call=1)
        client = CodexAppServerClient(
            process_factory=FakeFactory(process), close_timeout=0.001
        )
        start_task = asyncio.create_task(client.start())
        await process.stdin.drain_blocked.wait()

        await asyncio.wait_for(client.close(), 0.1)
        with pytest.raises(ProcessExitedError):
            await start_task
        await client.wait_closed()

        assert process.stdin.closed is True
        assert process.terminated == 1
        assert process.killed == 1
        assert client.pending_request_count == 0
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("codex-app-server-")
            and not task.done()
        ]

    run(scenario())


def test_writer_queued_before_close_rechecks_state_and_never_writes() -> None:
    async def scenario() -> None:
        process = BlockingWriteProcess(block_on_drain_call=3)
        client = CodexAppServerClient(
            process_factory=FakeFactory(process), close_timeout=0.001
        )
        await client.start()

        first = asyncio.create_task(client.request("first", {}, timeout=None))
        await process.stdin.drain_blocked.wait()
        second = asyncio.create_task(client.request("second", {}, timeout=None))
        await asyncio.sleep(0)

        await asyncio.wait_for(client.close(), 0.1)
        with pytest.raises(ProcessExitedError):
            await first
        with pytest.raises(ProcessExitedError):
            await second
        assert [message.get("method") for message in process.stdin.messages] == [
            "initialize",
            "initialized",
            "first",
        ]
        assert client.pending_request_count == 0

    run(scenario())


def test_failed_initialize_closes_process_and_raises_rpc_error() -> None:
    async def scenario() -> None:
        process = FakeProcess(auto_initialize=False)
        factory = FakeFactory(process)
        client = CodexAppServerClient(process_factory=factory)

        task = asyncio.create_task(client.start())
        await process.stdin.message_written.wait()
        request = process.stdin.messages[0]
        process.stdout.feed_json(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32000, "message": "unsupported", "data": {"feature": "x"}},
            }
        )

        with pytest.raises(ServerRpcError) as exc_info:
            await task
        assert exc_info.value.code == -32000
        assert exc_info.value.message == "unsupported"
        assert exc_info.value.data == {"feature": "x"}
        assert process.terminated == 1

    run(scenario())


def test_request_correlates_out_of_order_responses() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()

        first = asyncio.create_task(client.request("one", {"value": 1}))
        second = asyncio.create_task(client.request("two", {"value": 2}))
        await wait_for_message_count(process.stdin, 4)
        one, two = process.stdin.messages[-2:]
        process.stdout.feed_json({"jsonrpc": "2.0", "id": two["id"], "result": "second"})
        process.stdout.feed_json({"jsonrpc": "2.0", "id": one["id"], "result": "first"})

        assert await first == "first"
        assert await second == "second"
        assert client.pending_request_count == 0
        await client.close()

    run(scenario())


def test_request_timeout_removes_pending_future() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()

        with pytest.raises(TimeoutError):
            await client.request("never", {}, timeout=0.001)
        assert client.pending_request_count == 0
        await client.close()

    run(scenario())


def test_cancelled_request_removes_pending_future() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()

        pending = asyncio.create_task(client.request("cancel-me", {}, timeout=None))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert client.pending_request_count == 0
        await client.close()

    run(scenario())


def test_reader_routes_server_requests_and_notifications() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()

        process.stdout.feed_json(
            {"jsonrpc": "2.0", "id": "approval-1", "method": "item/commandExecution/requestApproval", "params": {"command": "x"}}
        )
        process.stdout.feed_json(
            {"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"id": "t1"}}}
        )

        request = await asyncio.wait_for(client.requests.get(), 0.1)
        notification = await asyncio.wait_for(client.notifications.get(), 0.1)
        assert request == ServerRequest(
            request_id="approval-1",
            method="item/commandExecution/requestApproval",
            params={"command": "x"},
        )
        assert notification == ServerNotification(
            method="turn/completed", params={"turn": {"id": "t1"}}
        )
        assert "'x'" not in repr(request)
        assert "'t1'" not in repr(notification)
        await client.close()

    run(scenario())


def test_stdout_stream_limit_allows_large_valid_line_then_rejects_oversized_line() -> None:
    async def scenario() -> None:
        max_line_bytes = 256 * 1024
        factory = StreamFactory()
        client = CodexAppServerClient(
            process_factory=factory, max_line_bytes=max_line_bytes
        )
        await client.start()
        assert factory.kwargs["limit"] > max_line_bytes
        assert factory.process is not None

        large_value = "x" * (100 * 1024)
        factory.process.stdout.feed_data(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "large/notification",
                    "params": {"value": large_value},
                }
            ).encode()
            + b"\n"
        )
        notification = await asyncio.wait_for(client.notifications.get(), 0.1)
        assert notification.params["value"] == large_value

        factory.process.stdout.feed_data(b"{" + b"x" * max_line_bytes + b"}\n")
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(client.wait_closed(), 0.1)
        assert factory.process.stdin.closed is True

    run(scenario())


@pytest.mark.parametrize(
    "bad_line",
    [
        b"not-json\n",
        json.dumps({"jsonrpc": "2.0", "result": {}}).encode() + b"\n",
        b"{" + (b"x" * (1024 * 1024)) + b"}\n",
    ],
    ids=["malformed-json", "invalid-envelope", "oversized-line"],
)
def test_protocol_failure_fails_pending_requests(bad_line: bytes) -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()
        pending = asyncio.create_task(client.request("pending", {}, timeout=0.05))
        await asyncio.sleep(0)

        process.stdout.feed(bad_line)

        with pytest.raises(ProtocolError):
            await pending
        assert client.pending_request_count == 0
        await client.close()

    run(scenario())


@pytest.mark.parametrize("queue_kind", ["request", "notification"])
def test_queue_overflow_is_terminal_protocol_error(queue_kind: str) -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(
            process_factory=FakeFactory(process),
            request_queue_capacity=1,
            notification_queue_capacity=1,
        )
        await client.start()
        if queue_kind == "request":
            process.stdout.feed_json(
                {"jsonrpc": "2.0", "id": "r1", "method": "approval", "params": {}}
            )
            process.stdout.feed_json(
                {"jsonrpc": "2.0", "id": "r2", "method": "approval", "params": {}}
            )
        else:
            process.stdout.feed_json(
                {"jsonrpc": "2.0", "method": "event", "params": {"n": 1}}
            )
            process.stdout.feed_json(
                {"jsonrpc": "2.0", "method": "event", "params": {"n": 2}}
            )

        with pytest.raises(ProtocolError, match="queue"):
            await asyncio.wait_for(client.wait_closed(), 0.1)
        assert process.stdin.closed is True

    run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"request_queue_capacity": 0},
        {"notification_queue_capacity": 0},
    ],
)
def test_queue_capacity_must_be_positive(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="capacity"):
        CodexAppServerClient(**kwargs)


def test_hybrid_response_and_request_envelope_is_protocol_error() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()
        pending = asyncio.create_task(client.request("pending", {}, timeout=0.05))
        await asyncio.sleep(0)

        process.stdout.feed_json(
            {
                "jsonrpc": "2.0",
                "id": "server-1",
                "method": "server/request",
                "params": {},
                "result": {},
            }
        )

        with pytest.raises(ProtocolError):
            await pending
        await client.close()

    run(scenario())


def test_process_wait_failure_fails_pending_request_without_stdout_eof() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()
        pending = asyncio.create_task(client.request("pending", {}))
        await asyncio.sleep(0)

        process.exit(9)

        with pytest.raises(ProcessExitedError) as exc_info:
            await pending
        assert exc_info.value.returncode == 9
        await client.close()

    run(scenario())


def test_request_detects_process_returncode_before_wait_task_runs() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()
        process.returncode = 11

        with pytest.raises(ProcessExitedError) as exc_info:
            await client.request("too-late", {}, timeout=0.01)
        assert exc_info.value.returncode == 11
        assert client.pending_request_count == 0
        await client.close()

    run(scenario())


def test_broken_stdin_is_reported_as_process_exit_and_cleans_pending() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()
        process.stdin.drain_error = BrokenPipeError()

        with pytest.raises(ProcessExitedError):
            await client.request("too-late", {}, timeout=0.01)
        assert client.pending_request_count == 0
        await client.close()

    run(scenario())


def test_eof_and_process_exit_fail_pending_requests() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()
        pending = asyncio.create_task(client.request("pending", {}))
        await asyncio.sleep(0)

        process.returncode = 7
        process.stdout.feed(b"")

        with pytest.raises(ProcessExitedError) as exc_info:
            await pending
        assert exc_info.value.returncode == 7
        await client.close()

    run(scenario())


def test_thread_turn_interrupt_and_server_responses() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()

        thread_task = asyncio.create_task(client.start_thread("C:/repo", model="gpt-x"))
        await wait_for_message_count(process.stdin, 3)
        thread_message = process.stdin.messages[-1]
        assert thread_message["params"] == {
            "approvalPolicy": "on-request",
            "cwd": "C:/repo",
            "sandbox": "workspace-write",
            "model": "gpt-x",
        }
        process.stdout.feed_json(
            {"jsonrpc": "2.0", "id": thread_message["id"], "result": {"thread": {"id": "th1"}}}
        )
        assert await thread_task == "th1"

        turn_task = asyncio.create_task(client.start_turn("th1", "do work"))
        await wait_for_message_count(process.stdin, 4)
        turn_message = process.stdin.messages[-1]
        assert turn_message["params"] == {
            "threadId": "th1",
            "input": [{"type": "text", "text": "do work"}],
        }
        process.stdout.feed_json(
            {"jsonrpc": "2.0", "id": turn_message["id"], "result": {"turn": {"id": "tu1"}}}
        )
        assert await turn_task == "tu1"

        interrupt_task = asyncio.create_task(client.interrupt_turn("th1", "tu1"))
        await wait_for_message_count(process.stdin, 5)
        interrupt_message = process.stdin.messages[-1]
        assert interrupt_message["method"] == "turn/interrupt"
        assert interrupt_message["params"] == {"threadId": "th1", "turnId": "tu1"}
        process.stdout.feed_json(
            {"jsonrpc": "2.0", "id": interrupt_message["id"], "result": {}}
        )
        await interrupt_task

        await client.respond("server-1", {"decision": "accept"})
        await client.respond_error("server-2", -32602, "bad input", {"field": "x"})
        assert process.stdin.messages[-2:] == [
            {"jsonrpc": "2.0", "id": "server-1", "result": {"decision": "accept"}},
            {
                "jsonrpc": "2.0",
                "id": "server-2",
                "error": {"code": -32602, "message": "bad input", "data": {"field": "x"}},
            },
        ]
        await client.close()

    run(scenario())


@pytest.mark.parametrize("sandbox", ["danger-full-access", "", "read_write"])
def test_start_thread_rejects_unsafe_sandbox(sandbox: str) -> None:
    async def scenario() -> None:
        client = CodexAppServerClient(process_factory=FakeFactory(FakeProcess()))
        with pytest.raises(ValueError, match="sandbox"):
            await client.start_thread("C:/repo", sandbox=sandbox)

    run(scenario())


def test_response_helpers_are_narrow_and_validate_user_input() -> None:
    assert command_approval_response(True) == {"decision": "accept"}
    assert command_approval_response(False) == {"decision": "decline"}
    assert file_approval_response(True) == {"decision": "accept"}
    assert file_approval_response(False) == {"decision": "decline"}

    params = {"permissions": {"network": {"enabled": True}}}
    assert permission_response(params, True) == {
        "permissions": {"network": {"enabled": True}},
        "scope": "turn",
        "strictAutoReview": False,
    }
    assert permission_response(params, False) == {
        "permissions": {},
        "scope": "turn",
        "strictAutoReview": False,
    }
    params["permissions"]["network"]["enabled"] = False
    assert permission_response({"permissions": {"network": {"enabled": True}}}, True)["permissions"]["network"]["enabled"] is True

    questions = [{"id": "q1"}, {"id": "q2"}]
    assert user_input_response(questions, {"q1": "yes", "q2": "no"}) == {
        "answers": {
            "q1": {"answers": ["yes"]},
            "q2": {"answers": ["no"]},
        }
    }
    with pytest.raises(ValueError, match="missing"):
        user_input_response(questions, {"q1": "yes"})
    with pytest.raises(ValueError, match="unknown"):
        user_input_response(questions, {"q1": "yes", "q2": "no", "q3": "maybe"})


def test_close_is_idempotent_and_stderr_is_bounded_and_redacted() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = CodexAppServerClient(
            process_factory=FakeFactory(process), stderr_tail_lines=2
        )
        await client.start()
        process.stderr.feed(b"first\n")
        process.stderr.feed(b"token=super-secret\n")
        process.stderr.feed(b"last\n")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert client.stderr_tail == ("token=[REDACTED]", "last")
        await client.close()
        await client.close()
        assert process.terminated == 1
        assert process.stdin.closed is True

    run(scenario())


def test_stderr_oversized_line_is_truncated_redacted_and_drain_continues() -> None:
    async def scenario() -> None:
        factory = StreamFactory()
        client = CodexAppServerClient(
            process_factory=factory,
            max_line_bytes=64 * 1024,
            stderr_tail_lines=2,
        )
        await client.start()
        assert factory.process is not None

        factory.process.stderr.feed_data(
            b"token=super-secret " + b"x" * (200 * 1024) + b"\nlast\n"
        )
        factory.process.stderr.feed_eof()
        for _ in range(10):
            if client.stderr_tail and client.stderr_tail[-1] == "last":
                break
            await asyncio.sleep(0)

        assert client.stderr_tail[-1] == "last"
        assert "super-secret" not in client.stderr_tail[0]
        assert "token=[REDACTED]" in client.stderr_tail[0]
        assert client.is_running is True
        await client.close()

    run(scenario())


def test_wait_closed_is_normal_for_explicit_close_and_preserves_terminal_error() -> None:
    async def scenario() -> None:
        normal = CodexAppServerClient(process_factory=FakeFactory(FakeProcess()))
        await normal.start()
        await normal.close()
        await normal.wait_closed()

        failed_process = FakeProcess()
        failed = CodexAppServerClient(process_factory=FakeFactory(failed_process))
        await failed.start()
        pending = asyncio.create_task(failed.request("pending", {}, timeout=None))
        await asyncio.sleep(0)
        failed_process.stdout.feed(b"not-json\n")
        with pytest.raises(ProtocolError) as pending_error:
            await pending
        with pytest.raises(ProtocolError) as terminal_error:
            await failed.wait_closed()
        assert terminal_error.value is pending_error.value

    run(scenario())


def test_close_tolerates_process_exit_racing_with_terminate() -> None:
    async def scenario() -> None:
        process = RacingExitProcess()
        client = CodexAppServerClient(process_factory=FakeFactory(process))
        await client.start()

        await client.close()

        assert process.terminated == 1
        assert process.stdin.closed is True

    run(scenario())


def test_async_context_manager_starts_and_closes() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        factory = FakeFactory(process)
        async with CodexAppServerClient(process_factory=factory) as client:
            assert client.is_running is True
        assert process.stdin.closed is True

    run(scenario())
