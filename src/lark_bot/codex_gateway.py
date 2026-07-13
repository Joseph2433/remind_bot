from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any, TypeAlias

from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from lark_bot.codex_app_server import ServerNotification, ServerRequest


JsonObject: TypeAlias = dict[str, Any]
RespondUpstream: TypeAlias = Callable[..., Awaitable[bool]]
ServerRequestHandler: TypeAlias = Callable[
    [ServerRequest, RespondUpstream], Awaitable[None]
]
TerminalResponseHandler: TypeAlias = Callable[
    [int | str, JsonObject, RespondUpstream], Awaitable[bool]
]
TerminalRequestObserver: TypeAlias = Callable[
    [int | str, str, JsonObject], Awaitable[None]
]
UpstreamResponseObserver: TypeAlias = Callable[
    [int | str, str, JsonObject, JsonObject], Awaitable[None]
]
UpstreamNotificationObserver: TypeAlias = Callable[
    [ServerNotification], Awaitable[None]
]


INTERCEPTED_SERVER_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/requestUserInput",
    }
)

DEFAULT_MAX_MESSAGE_SIZE = 1024 * 1024
DEFAULT_QUEUE_CAPACITY = 64
DEFAULT_CLOSE_TIMEOUT = 2.0


class CodexGateway:
    """A single-client, loopback JSON-RPC gateway for a Codex TUI."""

    def __init__(
        self,
        upstream_endpoint: str,
        *,
        on_server_request: ServerRequestHandler,
        on_terminal_response: TerminalResponseHandler,
        on_terminal_request: TerminalRequestObserver | None = None,
        on_upstream_response: UpstreamResponseObserver | None = None,
        on_upstream_notification: UpstreamNotificationObserver | None = None,
        token: str | None = None,
        upstream_headers: Mapping[str, str] | None = None,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
    ) -> None:
        if max_message_size <= 0:
            raise ValueError("max_message_size must be positive")
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")

        self._upstream_endpoint = upstream_endpoint
        self._on_server_request = on_server_request
        self._on_terminal_response = on_terminal_response
        self._on_terminal_request = on_terminal_request
        self._on_upstream_response = on_upstream_response
        self._on_upstream_notification = on_upstream_notification
        self._token = token or secrets.token_urlsafe(32)
        self._upstream_headers = dict(upstream_headers or {})
        self._max_message_size = max_message_size
        self._queue_capacity = queue_capacity
        self._close_timeout = close_timeout

        self._upstream: ClientConnection | None = None
        self._server: Server | None = None
        self._upstream_reader: asyncio.Task[None] | None = None
        self._callback_tasks: set[asyncio.Task[None]] = set()
        self._callback_tasks_by_request: dict[int | str, asyncio.Task[None]] = {}
        self._to_terminal: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_capacity)
        self._terminal: ServerConnection | None = None
        self._upstream_send_lock = asyncio.Lock()
        self._response_lock = asyncio.Lock()
        self._intercepted_ids: set[int | str] = set()
        self._responded_ids: set[int | str] = set()
        self._terminal_requests: dict[int | str, tuple[str, JsonObject]] = {}
        self._active_turns: dict[str, str] = {}
        self._started = False
        self._closing = False

    @property
    def endpoint(self) -> str:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("gateway is not started")
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    @property
    def token(self) -> str:
        """Bearer token suitable for Codex ``--remote-auth-token-env``."""
        return self._token

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("gateway is already started")
        self._started = True
        self._closing = False
        try:
            self._upstream = await connect(
                self._upstream_endpoint,
                additional_headers=self._upstream_headers or None,
                max_size=self._max_message_size,
                max_queue=self._queue_capacity,
                close_timeout=self._close_timeout,
                proxy=None,
            )
            self._server = await serve(
                self._handle_terminal,
                "127.0.0.1",
                0,
                process_request=self._authenticate,
                max_size=self._max_message_size,
                max_queue=self._queue_capacity,
                close_timeout=self._close_timeout,
            )
            self._upstream_reader = asyncio.create_task(
                self._read_upstream(), name="codex-gateway-upstream"
            )
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True

        server, self._server = self._server, None
        upstream, self._upstream = self._upstream, None
        reader, self._upstream_reader = self._upstream_reader, None
        callbacks = tuple(self._callback_tasks)
        self._callback_tasks.clear()
        self._callback_tasks_by_request.clear()
        self._active_turns.clear()

        if server is not None:
            server.close()
        for task in callbacks:
            task.cancel()
        if callbacks:
            await asyncio.gather(*callbacks, return_exceptions=True)
        if upstream is not None:
            await upstream.close()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

        self._terminal = None
        self._intercepted_ids.clear()
        self._responded_ids.clear()
        self._terminal_requests.clear()
        self._started = False

    def _authenticate(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self._token}"
        if secrets.compare_digest(supplied, expected):
            return None
        return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

    async def _handle_terminal(self, websocket: ServerConnection) -> None:
        if self._terminal is not None:
            await websocket.close(1008, "only one terminal client is allowed")
            return
        self._terminal = websocket
        sender = asyncio.create_task(
            self._send_to_terminal(websocket), name="codex-gateway-terminal-sender"
        )
        try:
            async for raw_message in websocket:
                if not isinstance(raw_message, str):
                    await websocket.close(1003, "text JSON messages required")
                    return
                message = self._parse_message(raw_message)
                if message is None:
                    await websocket.close(1007, "malformed JSON-RPC message")
                    return
                await self._handle_terminal_message(message, raw_message)
        except ConnectionClosed:
            pass
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            if self._terminal is websocket:
                self._terminal = None

    async def _send_to_terminal(self, websocket: ServerConnection) -> None:
        try:
            while True:
                await websocket.send(await self._to_terminal.get())
        except ConnectionClosed:
            pass

    async def _read_upstream(self) -> None:
        upstream = self._upstream
        if upstream is None:
            return
        try:
            async for raw_message in upstream:
                if not isinstance(raw_message, str):
                    await upstream.close(1003, "text JSON messages required")
                    await self._close_terminal(1003, "text JSON messages required")
                    return
                message = self._parse_message(raw_message)
                if message is None:
                    await upstream.close(1007, "malformed JSON-RPC message")
                    await self._close_terminal(1007, "malformed upstream JSON-RPC message")
                    return
                await self._handle_upstream_message(message, raw_message)
        except ConnectionClosed:
            await self._close_terminal(1011, "upstream connection closed")

    async def _handle_upstream_message(
        self, message: JsonObject, raw_message: str
    ) -> None:
        request_id = self._request_id(message)
        method = message.get("method")
        intercepted = (
            request_id is not None
            and isinstance(method, str)
            and method in INTERCEPTED_SERVER_METHODS
        )
        if intercepted:
            self._intercepted_ids.add(request_id)

        if method == "serverRequest/resolved":
            params = message.get("params")
            if isinstance(params, dict):
                resolved_id = params.get("requestId")
                if self._valid_id(resolved_id):
                    self._responded_ids.add(resolved_id)
                    callback = self._callback_tasks_by_request.pop(
                        resolved_id, None
                    )
                    if callback is not None:
                        callback.cancel()

        if request_id is None and method == "turn/started":
            params = message.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            turn = params.get("turn") if isinstance(params, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                self._active_turns[thread_id] = turn_id

        response_id = self._response_id(message)
        if response_id is not None:
            request = self._terminal_requests.pop(response_id, None)
            if request is not None and self._on_upstream_response is not None:
                await self._observe(
                    self._on_upstream_response(
                        response_id,
                        request[0],
                        request[1],
                        self._response_payload(message),
                    )
                )
        elif request_id is None and isinstance(method, str):
            params = message.get("params")
            if self._on_upstream_notification is not None:
                await self._observe(
                    self._on_upstream_notification(
                        ServerNotification(
                            method,
                            params if isinstance(params, dict) else {},
                        )
                    )
                )

        await self._to_terminal.put(raw_message)

        if intercepted:
            params = message.get("params")
            request_params = dict(params) if isinstance(params, dict) else {}
            thread_id = request_params.get("threadId")
            if isinstance(thread_id, str) and "turnId" not in request_params:
                turn_id = self._active_turns.get(thread_id)
                if turn_id is not None:
                    request_params["turnId"] = turn_id
            request = ServerRequest(
                request_id=request_id,
                method=method,
                params=request_params,
            )
            task = asyncio.create_task(
                self._run_server_request_callback(request),
                name=f"codex-gateway-request-{request_id}",
            )
            self._callback_tasks.add(task)
            self._callback_tasks_by_request[request_id] = task
            task.add_done_callback(
                lambda completed, current_id=request_id: self._callback_finished(
                    current_id, completed
                )
            )

        if request_id is None and method == "turn/completed":
            params = message.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            turn = params.get("turn") if isinstance(params, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if (
                isinstance(thread_id, str)
                and isinstance(turn_id, str)
                and self._active_turns.get(thread_id) == turn_id
            ):
                self._active_turns.pop(thread_id, None)

    async def _run_server_request_callback(self, request: ServerRequest) -> None:
        if request.request_id in self._responded_ids:
            return
        try:
            await self._on_server_request(request, self.respond_upstream)
        except Exception:
            # The terminal still owns the pending request if the side channel fails.
            return

    def _callback_finished(
        self, request_id: int | str, task: asyncio.Task[None]
    ) -> None:
        self._callback_tasks.discard(task)
        if self._callback_tasks_by_request.get(request_id) is task:
            self._callback_tasks_by_request.pop(request_id, None)

    async def _handle_terminal_message(
        self, message: JsonObject, raw_message: str
    ) -> None:
        outgoing_id = self._request_id(message)
        if outgoing_id is not None:
            method = message["method"]
            params = message.get("params")
            normalized_params = params if isinstance(params, dict) else {}
            self._terminal_requests[outgoing_id] = (method, normalized_params)
            if self._on_terminal_request is not None:
                await self._observe(
                    self._on_terminal_request(
                        outgoing_id, method, normalized_params
                    )
                )

        request_id = self._response_id(message)
        if request_id is None or request_id not in self._intercepted_ids:
            await self._send_upstream(raw_message)
            return

        result_or_error: JsonObject
        if "result" in message:
            result_or_error = {"result": message["result"]}
        else:
            result_or_error = {"error": message["error"]}
        try:
            await self._on_terminal_response(
                request_id, result_or_error, self.respond_upstream
            )
        except Exception:
            return

    @staticmethod
    async def _observe(callback: Awaitable[None]) -> None:
        try:
            await callback
        except Exception:
            # Observers are a side channel and must never break JSON-RPC forwarding.
            return

    @staticmethod
    def _response_payload(message: JsonObject) -> JsonObject:
        if "result" in message:
            return {"result": message["result"]}
        return {"error": message.get("error")}

    async def respond_upstream(
        self,
        request_id: int | str,
        result: Any = None,
        *,
        error: JsonObject | None = None,
    ) -> bool:
        """Resolve an intercepted request once; return whether this call won."""
        if error is not None:
            payload: JsonObject = {"error": error}
        else:
            payload = {"result": result}
        return await self._respond_with_payload(request_id, payload)

    async def _respond_with_payload(
        self, request_id: int | str, payload: JsonObject
    ) -> bool:
        async with self._response_lock:
            if (
                request_id not in self._intercepted_ids
                or request_id in self._responded_ids
            ):
                return False
            self._responded_ids.add(request_id)
            response = {"jsonrpc": "2.0", "id": request_id, **payload}
            try:
                await self._send_upstream(
                    json.dumps(response, separators=(",", ":"), ensure_ascii=False)
                )
            except BaseException:
                self._responded_ids.discard(request_id)
                raise
            return True

    async def _send_upstream(self, message: str) -> None:
        upstream = self._upstream
        if upstream is None:
            raise RuntimeError("upstream is not connected")
        async with self._upstream_send_lock:
            await upstream.send(message)

    async def _close_terminal(self, code: int, reason: str) -> None:
        terminal = self._terminal
        if terminal is not None:
            await terminal.close(code, reason)

    @staticmethod
    def _parse_message(raw_message: str) -> JsonObject | None:
        try:
            message = json.loads(raw_message)
        except (json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(message, dict):
            return None
        if "jsonrpc" in message and message["jsonrpc"] != "2.0":
            return None
        return message

    @classmethod
    def _request_id(cls, message: JsonObject) -> int | str | None:
        if not isinstance(message.get("method"), str):
            return None
        request_id = message.get("id")
        return request_id if cls._valid_id(request_id) else None

    @classmethod
    def _response_id(cls, message: JsonObject) -> int | str | None:
        if "method" in message or ("result" not in message and "error" not in message):
            return None
        request_id = message.get("id")
        return request_id if cls._valid_id(request_id) else None

    @staticmethod
    def _valid_id(value: object) -> bool:
        return isinstance(value, (int, str)) and not isinstance(value, bool)
