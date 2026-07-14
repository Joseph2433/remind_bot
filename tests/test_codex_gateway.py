from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from lark_bot.codex_app_server import ServerRequest
from lark_bot.codex_gateway import CodexGateway


JsonObject = dict[str, Any]


class FakeUpstream:
    def __init__(self) -> None:
        self.received: asyncio.Queue[JsonObject] = asyncio.Queue()
        self.clients: asyncio.Queue[ServerConnection] = asyncio.Queue()
        self.closed = asyncio.Event()
        self._server: Any = None

    async def __aenter__(self) -> "FakeUpstream":
        async def handler(websocket: ServerConnection) -> None:
            await self.clients.put(websocket)
            try:
                async for message in websocket:
                    await self.received.put(json.loads(message))
            finally:
                self.closed.set()

        self._server = await serve(handler, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def endpoint(self) -> str:
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    async def client(self) -> ServerConnection:
        return await asyncio.wait_for(self.clients.get(), timeout=1)

    async def next_received(self) -> JsonObject:
        return await asyncio.wait_for(self.received.get(), timeout=1)


async def noop_server_request(
    request: ServerRequest,
    respond_upstream: Callable[..., Awaitable[bool]],
) -> None:
    del request, respond_upstream


async def allow_terminal_response(
    request_id: int | str,
    result_or_error: JsonObject,
    respond_upstream: Callable[..., Awaitable[bool]],
) -> bool:
    if "result" in result_or_error:
        return await respond_upstream(request_id, result=result_or_error["result"])
    return await respond_upstream(request_id, error=result_or_error["error"])


def test_transparently_forwards_regular_json_rpc_request_and_response() -> None:
    async def scenario() -> None:
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            upstream_socket = await upstream.client()
            try:
                async with connect(
                    gateway.endpoint,
                    additional_headers={"Authorization": "Bearer test-token"},
                ) as terminal:
                    request = {"jsonrpc": "2.0", "id": 7, "method": "thread/list", "params": {}}
                    await terminal.send(json.dumps(request))
                    assert await upstream.next_received() == request

                    response = {"jsonrpc": "2.0", "id": 7, "result": {"data": []}}
                    await upstream_socket.send(json.dumps(response))
                    assert json.loads(await asyncio.wait_for(terminal.recv(), 1)) == response
            finally:
                await gateway.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method",
    [
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/requestUserInput",
    ],
)
def test_terminal_winner_for_intercepted_server_request_is_forwarded_once(method: str) -> None:
    async def scenario() -> None:
        seen_requests: list[ServerRequest] = []
        seen_terminal: list[tuple[int | str, JsonObject]] = []

        async def on_request(request: ServerRequest, respond: Callable[..., Awaitable[bool]]) -> None:
            del respond
            seen_requests.append(request)

        async def on_terminal(
            request_id: int | str,
            result_or_error: JsonObject,
            respond: Callable[..., Awaitable[bool]],
        ) -> bool:
            seen_terminal.append((request_id, result_or_error))
            return await respond(request_id, result=result_or_error["result"])

        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=on_request,
                on_terminal_response=on_terminal,
                token="test-token",
            )
            await gateway.start()
            upstream_socket = await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    request = {
                        "jsonrpc": "2.0",
                        "id": "approval-1",
                        "method": method,
                        "params": {"command": "echo ok"},
                    }
                    await upstream_socket.send(json.dumps(request))
                    assert json.loads(await asyncio.wait_for(terminal.recv(), 1)) == request
                    await asyncio.wait_for(_wait_until(lambda: bool(seen_requests)), 1)
                    assert seen_requests == [
                        ServerRequest("approval-1", method, {"command": "echo ok"})
                    ]

                    response = {"jsonrpc": "2.0", "id": "approval-1", "result": {"decision": "accept"}}
                    await terminal.send(json.dumps(response))
                    assert await upstream.next_received() == response
                    assert seen_terminal == [("approval-1", {"result": {"decision": "accept"}})]
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_server_request_callback_is_bound_to_active_turn() -> None:
    async def scenario() -> None:
        seen_requests: list[ServerRequest] = []

        async def on_request(
            request: ServerRequest,
            respond: Callable[..., Awaitable[bool]],
        ) -> None:
            del respond
            seen_requests.append(request)

        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=on_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            upstream_socket = await upstream.client()
            try:
                async with connect(
                    gateway.endpoint,
                    additional_headers={"Authorization": "Bearer test-token"},
                ) as terminal:
                    started = {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1"},
                        },
                    }
                    await upstream_socket.send(json.dumps(started))
                    assert json.loads(await asyncio.wait_for(terminal.recv(), 1)) == started

                    request = {
                        "jsonrpc": "2.0",
                        "id": "approval-1",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "thread-1"},
                    }
                    await upstream_socket.send(json.dumps(request))
                    assert json.loads(await asyncio.wait_for(terminal.recv(), 1)) == request
                    await asyncio.wait_for(
                        _wait_until(lambda: bool(seen_requests)), 1
                    )
                    assert seen_requests == [
                        ServerRequest(
                            "approval-1",
                            "item/commandExecution/requestApproval",
                            {"threadId": "thread-1", "turnId": "turn-1"},
                        )
                    ]
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_lark_winner_suppresses_late_terminal_response() -> None:
    async def scenario() -> None:
        async def on_request(request: ServerRequest, respond: Callable[..., Awaitable[bool]]) -> None:
            assert await respond(request.request_id, result={"decision": "decline"}) is True

        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=on_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            upstream_socket = await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    request = {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "item/fileChange/requestApproval",
                        "params": {"path": "x"},
                    }
                    await upstream_socket.send(json.dumps(request))
                    assert json.loads(await asyncio.wait_for(terminal.recv(), 1)) == request
                    assert await upstream.next_received() == {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "result": {"decision": "decline"},
                    }

                    await terminal.send(json.dumps({"jsonrpc": "2.0", "id": 9, "result": {"decision": "accept"}}))
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(upstream.received.get(), timeout=0.1)
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_resolved_notification_is_forwarded_to_terminal() -> None:
    async def scenario() -> None:
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            upstream_socket = await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    notification = {
                        "jsonrpc": "2.0",
                        "method": "serverRequest/resolved",
                        "params": {"requestId": "approval-1"},
                    }
                    await upstream_socket.send(json.dumps(notification))
                    assert json.loads(await asyncio.wait_for(terminal.recv(), 1)) == notification
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_slow_side_channel_callback_does_not_block_upstream_notifications() -> None:
    async def scenario() -> None:
        callback_started = asyncio.Event()
        release_callback = asyncio.Event()
        callback_completed = asyncio.Event()

        async def on_request(request: ServerRequest, respond: Callable[..., Awaitable[bool]]) -> None:
            del request, respond
            callback_started.set()
            await release_callback.wait()
            callback_completed.set()

        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=on_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            upstream_socket = await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    request = {
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "item/permissions/requestApproval",
                        "params": {},
                    }
                    notification = {
                        "jsonrpc": "2.0",
                        "method": "serverRequest/resolved",
                        "params": {"requestId": 12},
                    }
                    await upstream_socket.send(json.dumps(request))
                    assert json.loads(await asyncio.wait_for(terminal.recv(), 1)) == request
                    await asyncio.wait_for(callback_started.wait(), 1)
                    await upstream_socket.send(json.dumps(notification))
                    assert json.loads(await asyncio.wait_for(terminal.recv(), 1)) == notification
                    release_callback.set()
                    await asyncio.sleep(0.05)
                    assert not callback_completed.is_set()
            finally:
                release_callback.set()
                await gateway.close()

    asyncio.run(scenario())


def test_rejects_terminal_without_matching_bearer_token() -> None:
    async def scenario() -> None:
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="expected-token",
            )
            await gateway.start()
            await upstream.client()
            try:
                with pytest.raises(InvalidStatus) as rejected:
                    async with connect(
                        gateway.endpoint,
                        additional_headers={"Authorization": "Bearer wrong-token"},
                    ) as terminal:
                        await terminal.recv()
                assert rejected.value.response.status_code == 401
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_rejects_second_terminal_client() -> None:
    async def scenario() -> None:
        headers = {"Authorization": "Bearer test-token"}
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers=headers):
                    async with connect(gateway.endpoint, additional_headers=headers) as second:
                        with pytest.raises(ConnectionClosed) as closed:
                            await second.recv()
                        assert closed.value.rcvd is not None
                        assert closed.value.rcvd.code == 1008
                        assert closed.value.rcvd.reason == (
                            "session picker unsupported; use resume --last, "
                            "an explicit session ID, or --no-lark"
                        )
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_malformed_terminal_json_closes_safely_without_forwarding() -> None:
    async def scenario() -> None:
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    await terminal.send("{")
                    with pytest.raises(ConnectionClosed) as closed:
                        await terminal.recv()
                    assert closed.value.rcvd is not None
                    assert closed.value.rcvd.code == 1007
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(upstream.received.get(), timeout=0.1)
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_closes_terminal_that_sends_oversize_message() -> None:
    async def scenario() -> None:
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
                max_message_size=128,
            )
            await gateway.start()
            await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    await terminal.send(json.dumps({"jsonrpc": "2.0", "method": "x", "params": {"text": "x" * 256}}))
                    with pytest.raises(ConnectionClosed) as closed:
                        await terminal.recv()
                    assert closed.value.rcvd is not None
                    assert closed.value.rcvd.code == 1009
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(upstream.received.get(), timeout=0.1)
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_accepts_codex_message_when_jsonrpc_field_is_omitted() -> None:
    async def scenario() -> None:
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    response = {"id": 1, "result": {"userAgent": "codex_cli_rs"}}
                    await terminal.send(json.dumps(response))
                    assert await upstream.next_received() == response
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_rejects_explicit_non_2_jsonrpc_version() -> None:
    async def scenario() -> None:
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    await terminal.send(json.dumps({"jsonrpc": "1.0", "id": 1, "result": {}}))
                    with pytest.raises(ConnectionClosed) as closed:
                        await terminal.recv()
                    assert closed.value.rcvd is not None
                    assert closed.value.rcvd.code == 1007
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(upstream.received.get(), timeout=0.1)
            finally:
                await gateway.close()

    asyncio.run(scenario())


def test_close_releases_listener_and_upstream_connection() -> None:
    async def scenario() -> None:
        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                token="test-token",
            )
            await gateway.start()
            await upstream.client()
            endpoint = gateway.endpoint
            await gateway.close()
            await asyncio.wait_for(upstream.closed.wait(), timeout=1)
            with pytest.raises(OSError):
                async with connect(endpoint, additional_headers={"Authorization": "Bearer test-token"}):
                    pass

    asyncio.run(scenario())


def test_observer_correlates_thread_start_response_and_notifications() -> None:
    async def scenario() -> None:
        terminal_requests = []
        upstream_responses = []
        notifications = []

        async def on_terminal_request(request_id, method, params):
            terminal_requests.append((request_id, method, params))

        async def on_upstream_response(request_id, method, params, payload):
            upstream_responses.append((request_id, method, params, payload))

        async def on_notification(notification):
            notifications.append(notification)

        async with FakeUpstream() as upstream:
            gateway = CodexGateway(
                upstream.endpoint,
                on_server_request=noop_server_request,
                on_terminal_response=allow_terminal_response,
                on_terminal_request=on_terminal_request,
                on_upstream_response=on_upstream_response,
                on_upstream_notification=on_notification,
                token="test-token",
            )
            await gateway.start()
            upstream_socket = await upstream.client()
            try:
                async with connect(gateway.endpoint, additional_headers={"Authorization": "Bearer test-token"}) as terminal:
                    request = {"id": 41, "method": "thread/start", "params": {"cwd": "C:/work"}}
                    await terminal.send(json.dumps(request))
                    assert await upstream.next_received() == request
                    await _wait_until(lambda: bool(terminal_requests))

                    response = {"id": 41, "result": {"thread": {"id": "thread-1"}}}
                    await upstream_socket.send(json.dumps(response))
                    assert json.loads(await terminal.recv()) == response

                    notification = {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}}
                    await upstream_socket.send(json.dumps(notification))
                    assert json.loads(await terminal.recv()) == notification
                    await _wait_until(lambda: bool(upstream_responses) and bool(notifications))

                    assert terminal_requests == [(41, "thread/start", {"cwd": "C:/work"})]
                    assert upstream_responses == [(41, "thread/start", {"cwd": "C:/work"}, {"result": {"thread": {"id": "thread-1"}}})]
                    assert notifications[0].method == "turn/completed"
            finally:
                await gateway.close()

    asyncio.run(scenario())


async def _wait_until(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0)
