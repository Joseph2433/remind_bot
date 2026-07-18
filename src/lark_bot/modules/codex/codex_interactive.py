from __future__ import annotations

import asyncio
import shutil
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from lark_bot.modules.codex.app_server import ServerNotification, ServerRequest
from lark_bot.modules.codex.codex_gateway import CodexGateway


@dataclass(frozen=True, slots=True)
class InteractiveSessionDescriptor:
    session_id: str
    endpoint: str
    remote_auth_token: str


@dataclass(slots=True)
class _InteractiveRuntime:
    process: Any
    gateway: Any


ProcessFactory = Callable[..., Awaitable[Any]]
GatewayFactory = Callable[..., Any]
ListenerWaiter = Callable[[str, Any], Awaitable[None]]


async def _default_process_factory(*args: object, **kwargs: object) -> Any:
    return await asyncio.create_subprocess_exec(*args, **kwargs)


def _loopback_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return f"ws://127.0.0.1:{port}"


async def _wait_for_listener(
    endpoint: str,
    process: Any,
    *,
    timeout: float = 5.0,
) -> None:
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise RuntimeError("invalid Codex app-server endpoint")
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if process.returncode is not None:
            raise RuntimeError(
                f"Codex app-server exited during startup ({process.returncode})"
            )
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Codex app-server listener did not become ready")
            await asyncio.sleep(0.05)
            continue
        del reader
        writer.close()
        await writer.wait_closed()
        return


class InteractiveSessionManager:
    """Own one external app-server and gateway for each native TUI session."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        codex_path: str = "codex",
        process_factory: ProcessFactory = _default_process_factory,
        gateway_factory: GatewayFactory = CodexGateway,
        which: Callable[[str], str | None] = shutil.which,
        endpoint_factory: Callable[[], str] = _loopback_endpoint,
        wait_listener: ListenerWaiter = _wait_for_listener,
        close_timeout: float = 2.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._codex_path = codex_path
        self._process_factory = process_factory
        self._gateway_factory = gateway_factory
        self._which = which
        self._endpoint_factory = endpoint_factory
        self._wait_listener = wait_listener
        self._close_timeout = close_timeout
        self._sessions: dict[str, _InteractiveRuntime] = {}
        self._started = False
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("interactive session manager is closed")
        self._started = True

    async def create_session(
        self,
        *,
        name: str,
        cwd: str,
        model: str | None = None,
        sandbox: str = "workspace-write",
    ) -> InteractiveSessionDescriptor:
        if not self._started or self._closed:
            raise RuntimeError("interactive session manager is not running")
        executable = self._which(self._codex_path)
        if executable is None:
            raise RuntimeError("Codex executable is unavailable")
        upstream_endpoint = self._endpoint_factory()
        self._validate_loopback(upstream_endpoint)

        process = await self._process_factory(
            executable,
            "app-server",
            "--listen",
            upstream_endpoint,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        gateway: Any | None = None
        session_id: str | None = None
        try:
            await self._wait_listener(upstream_endpoint, process)
            session = await self._orchestrator.create_interactive_session(
                name, cwd, model, sandbox
            )
            session_id = session.id

            async def on_terminal_request(
                request_id: int | str, method: str, params: dict[str, Any]
            ) -> None:
                # Correlation is retained by CodexGateway. Binding happens only
                # after the upstream app-server confirms success.
                del request_id, method, params

            async def on_upstream_response(
                request_id: int | str,
                method: str,
                params: dict[str, Any],
                payload: dict[str, Any],
            ) -> None:
                del request_id
                if "result" not in payload or "error" in payload:
                    return
                result = payload.get("result")
                if method == "thread/start":
                    thread = result.get("thread") if isinstance(result, dict) else None
                    thread_id = thread.get("id") if isinstance(thread, dict) else None
                elif method == "thread/resume":
                    thread_id = params.get("threadId")
                else:
                    return
                if isinstance(thread_id, str) and thread_id:
                    self._orchestrator.bind_interactive_thread(session_id, thread_id)

            async def on_server_request(
                request: ServerRequest, responder: Callable[..., Awaitable[bool]]
            ) -> None:
                await self._orchestrator.process_server_request(
                    request, session_id=session_id, responder=responder
                )

            async def on_terminal_response(
                request_id: int | str,
                result_or_error: dict[str, Any],
                responder: Callable[..., Awaitable[bool]],
            ) -> bool:
                result = result_or_error.get("result")
                if "result" not in result_or_error:
                    error = result_or_error.get("error")
                    return await responder(
                        request_id,
                        error=error if isinstance(error, dict) else {},
                    )
                return await self._orchestrator.resolve_terminal_request(
                    request_id, result
                )

            async def on_notification(notification: ServerNotification) -> None:
                await self._orchestrator.process_notification(notification)

            gateway = self._gateway_factory(
                upstream_endpoint,
                on_server_request=on_server_request,
                on_terminal_response=on_terminal_response,
                on_terminal_request=on_terminal_request,
                on_upstream_response=on_upstream_response,
                on_upstream_notification=on_notification,
            )
            await gateway.start()
            async with self._lock:
                self._sessions[session_id] = _InteractiveRuntime(process, gateway)
            return InteractiveSessionDescriptor(
                session_id=session_id,
                endpoint=gateway.endpoint,
                remote_auth_token=gateway.token,
            )
        except BaseException:
            if gateway is not None:
                await gateway.close()
            await self._stop_process(process)
            if session_id is not None:
                await self._orchestrator.close_interactive_session(session_id)
            raise

    async def close_session(self, session_id: str) -> bool:
        async with self._lock:
            runtime = self._sessions.pop(session_id, None)
        if runtime is None:
            return False
        await runtime.gateway.close()
        await self._stop_process(runtime.process)
        await self._orchestrator.close_interactive_session(session_id)
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for session_id in tuple(self._sessions):
            try:
                await self.close_session(session_id)
            except BaseException:
                continue

    async def _stop_process(self, process: Any) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._close_timeout)
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    def _validate_loopback(endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("Codex app-server endpoint must use loopback WebSocket")
        if parsed.port is None:
            raise RuntimeError("Codex app-server endpoint must include a port")
