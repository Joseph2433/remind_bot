from __future__ import annotations

import asyncio
import copy
import json
import shutil
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Protocol

from lark_bot import __version__
from lark_bot.redaction import redact_text


MAX_STDOUT_LINE_BYTES = 1024 * 1024
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_CLOSE_TIMEOUT = 2.0
DEFAULT_QUEUE_CAPACITY = 100
_STDERR_ENTRY_BYTES = 4096
_ALLOWED_SANDBOXES = frozenset({"read-only", "workspace-write"})


class ProtocolError(RuntimeError):
    """The app-server emitted malformed or unsupported protocol data."""


class ServerRpcError(RuntimeError):
    """The app-server returned a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"Codex app-server RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class ProcessExitedError(RuntimeError):
    """The app-server process or its stdout stream exited."""

    def __init__(self, returncode: int | None, message: str | None = None) -> None:
        detail = message or "Codex app-server process exited"
        if returncode is not None:
            detail = f"{detail} with return code {returncode}"
        super().__init__(detail)
        self.returncode = returncode


@dataclass(frozen=True, slots=True)
class ServerRequest:
    request_id: int | str
    method: str
    params: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ServerNotification:
    method: str
    params: dict[str, Any] = field(repr=False)


class _Reader(Protocol):
    async def readline(self) -> bytes: ...

    async def read(self, size: int = -1) -> bytes: ...


class _Writer(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class _Process(Protocol):
    stdin: _Writer
    stdout: _Reader
    stderr: _Reader
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[_Process]]


async def _default_process_factory(*args: object, **kwargs: object) -> _Process:
    command = list(args)
    if command and isinstance(command[0], str):
        command[0] = shutil.which(command[0]) or command[0]
    return await asyncio.create_subprocess_exec(  # type: ignore[arg-type, return-value]
        *command,
        **kwargs,
    )


def command_approval_response(allow: bool) -> dict[str, str]:
    return {"decision": "accept" if allow else "decline"}


def file_approval_response(allow: bool) -> dict[str, str]:
    return {"decision": "accept" if allow else "decline"}


def permission_response(params: Mapping[str, Any], allow: bool) -> dict[str, Any]:
    requested = params.get("permissions", {})
    if not isinstance(requested, Mapping):
        raise ValueError("permissions must be an object")
    return {
        "permissions": copy.deepcopy(dict(requested)) if allow else {},
        "scope": "turn",
        "strictAutoReview": False,
    }


def user_input_response(
    questions: Sequence[Mapping[str, Any]], answers: Mapping[str, str]
) -> dict[str, dict[str, dict[str, list[str]]]]:
    question_ids: list[str] = []
    for question in questions:
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("each question must have a non-empty string id")
        if question_id in question_ids:
            raise ValueError(f"duplicate question id: {question_id}")
        question_ids.append(question_id)

    expected = set(question_ids)
    provided = set(answers)
    missing = expected - provided
    unknown = provided - expected
    if missing:
        raise ValueError(f"missing answers for question ids: {sorted(missing)}")
    if unknown:
        raise ValueError(f"unknown question ids: {sorted(unknown)}")
    if any(not isinstance(value, str) for value in answers.values()):
        raise ValueError("answers must be strings")

    return {
        "answers": {
            question_id: {"answers": [answers[question_id]]}
            for question_id in question_ids
        }
    }


class _Lifecycle(Enum):
    NEW = auto()
    STARTING = auto()
    RUNNING = auto()
    CLOSING = auto()
    CLOSED = auto()


class CodexAppServerClient:
    def __init__(
        self,
        *,
        process_factory: ProcessFactory = _default_process_factory,
        codex_path: str = "codex",
        max_line_bytes: int = MAX_STDOUT_LINE_BYTES,
        stderr_tail_lines: int = 50,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
        request_queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        notification_queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
    ) -> None:
        if max_line_bytes <= 0:
            raise ValueError("max_line_bytes must be positive")
        if stderr_tail_lines <= 0:
            raise ValueError("stderr_tail_lines must be positive")
        if request_queue_capacity <= 0 or notification_queue_capacity <= 0:
            raise ValueError("queue capacity must be positive")
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")

        self._process_factory = process_factory
        self._codex_path = codex_path
        self._max_line_bytes = max_line_bytes
        self._close_timeout = close_timeout
        self._process: _Process | None = None
        self._next_request_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._requests: asyncio.Queue[ServerRequest] = asyncio.Queue(
            maxsize=request_queue_capacity
        )
        self._notifications: asyncio.Queue[ServerNotification] = asyncio.Queue(
            maxsize=notification_queue_capacity
        )
        self._stderr_tail: deque[str] = deque(maxlen=stderr_tail_lines)
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._write_operations: set[asyncio.Task[None]] = set()
        self._drain_tasks: set[asyncio.Task[None]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._closed_event = asyncio.Event()
        self._lifecycle = _Lifecycle.NEW
        self._explicit_close = False
        self._terminal_error: BaseException | None = None
        self._closing_error: BaseException | None = None

    @property
    def requests(self) -> asyncio.Queue[ServerRequest]:
        return self._requests

    @property
    def notifications(self) -> asyncio.Queue[ServerNotification]:
        return self._notifications

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr_tail)

    @property
    def pending_request_count(self) -> int:
        return len(self._pending)

    @property
    def is_running(self) -> bool:
        process = self._process
        return (
            self._lifecycle is _Lifecycle.RUNNING
            and self._terminal_error is None
            and process is not None
            and process.returncode is None
        )

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._lifecycle is _Lifecycle.RUNNING:
                return
            if self._lifecycle in {_Lifecycle.CLOSING, _Lifecycle.CLOSED}:
                raise RuntimeError("Codex app-server client is closed")
            if self._startup_task is None:
                self._lifecycle = _Lifecycle.STARTING
                self._startup_task = asyncio.create_task(
                    self._start_impl(), name="codex-app-server-startup"
                )
                self._startup_task.add_done_callback(self._consume_task_exception)
            startup_task = self._startup_task
        try:
            await asyncio.shield(startup_task)
        except BaseException:
            if startup_task.done():
                cleanup_task = self._cleanup_task
                if cleanup_task is not None:
                    await asyncio.shield(cleanup_task)
            raise

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        await self._ensure_available()
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                }
            )
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except BaseException:
            if future.done() and not future.cancelled():
                future.exception()
            raise
        finally:
            self._pending.pop(request_id, None)

    async def start_thread(
        self,
        cwd: str,
        model: str | None = None,
        sandbox: str = "workspace-write",
    ) -> str:
        if sandbox not in _ALLOWED_SANDBOXES:
            raise ValueError("sandbox must be one of: read-only, workspace-write")
        params: dict[str, Any] = {
            "approvalPolicy": "on-request",
            "cwd": cwd,
            "sandbox": sandbox,
        }
        if model is not None:
            params["model"] = model
        result = await self.request("thread/start", params)
        return self._nested_id(result, "thread")

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        result = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        return self._nested_id(result, "turn")

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )

    async def respond(self, request_id: int | str, result: object) -> None:
        await self._ensure_available()
        await self._write_message(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    async def respond_error(
        self,
        request_id: int | str,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None:
        await self._ensure_available()
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._write_message(
            {"jsonrpc": "2.0", "id": request_id, "error": error}
        )

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._lifecycle is _Lifecycle.CLOSED:
                cleanup_task = self._cleanup_task
            else:
                self._explicit_close = True
                self._lifecycle = _Lifecycle.CLOSING
                if self._closing_error is None:
                    self._closing_error = ProcessExitedError(
                        self._returncode(), "client closed"
                    )
                self._fail_pending(self._closing_error)
                cleanup_task = self._ensure_cleanup_task_locked()
        if cleanup_task is not None:
            await asyncio.shield(cleanup_task)

    async def wait_closed(self) -> None:
        await self._closed_event.wait()
        if self._terminal_error is not None:
            raise self._terminal_error

    async def __aenter__(self) -> CodexAppServerClient:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()

    async def _start_impl(self) -> None:
        try:
            process = await self._process_factory(
                self._codex_path,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self._max_line_bytes + 1,
            )
            self._process = process
            async with self._lifecycle_lock:
                if self._lifecycle is _Lifecycle.CLOSING:
                    raise RuntimeError("Codex app-server client is closed")

            self._stdout_task = asyncio.create_task(
                self._read_stdout(), name="codex-app-server-stdout"
            )
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(), name="codex-app-server-stderr"
            )
            self._wait_task = asyncio.create_task(
                self._wait_for_process(), name="codex-app-server-wait"
            )
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "lark-bot",
                        "title": "Lark Bot",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            async with self._lifecycle_lock:
                if self._lifecycle is _Lifecycle.CLOSING:
                    raise RuntimeError("Codex app-server client is closed")
            await self._write_message({"jsonrpc": "2.0", "method": "initialized"})
            async with self._lifecycle_lock:
                if self._lifecycle is _Lifecycle.CLOSING:
                    raise RuntimeError("Codex app-server client is closed")
                self._lifecycle = _Lifecycle.RUNNING
        except BaseException as exc:
            if not self._explicit_close:
                await self._record_terminal_error(exc)
            raise

    async def _write_message(self, message: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            message, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8") + b"\n"
        allow_starting = asyncio.current_task() is self._startup_task
        operation = asyncio.create_task(
            self._write_impl(encoded, allow_starting),
            name="codex-app-server-write",
        )
        self._write_operations.add(operation)
        operation.add_done_callback(self._write_operations.discard)
        await operation

    async def _write_impl(self, encoded: bytes, allow_starting: bool) -> None:
        try:
            async with self._write_lock:
                process = await self._process_for_write(allow_starting)
                process.stdin.write(encoded)
                drain_task = asyncio.create_task(
                    process.stdin.drain(), name="codex-app-server-drain"
                )
                self._drain_tasks.add(drain_task)
                try:
                    await drain_task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    raise self._closing_exception()
                finally:
                    self._drain_tasks.discard(drain_task)
        except (BrokenPipeError, ConnectionResetError) as exc:
            error = ProcessExitedError(self._returncode())
            await self._record_terminal_error(error)
            raise error from exc

    async def _process_for_write(self, allow_starting: bool) -> _Process:
        async with self._lifecycle_lock:
            if self._lifecycle is _Lifecycle.CLOSING:
                raise self._closing_exception()
            if self._lifecycle is _Lifecycle.CLOSED:
                self._raise_terminal_error()
                raise RuntimeError("Codex app-server client is closed")
            if self._lifecycle is _Lifecycle.NEW or self._process is None:
                raise RuntimeError("Codex app-server client is not started")
            if self._lifecycle is _Lifecycle.STARTING and not allow_starting:
                raise RuntimeError("Codex app-server client is still starting")
            if self._process.returncode is not None:
                raise ProcessExitedError(self._process.returncode)
            return self._process

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError as exc:
                    raise ProtocolError("app-server output line exceeded limit") from exc
                if not line:
                    raise ProcessExitedError(self._returncode())
                if len(line) > self._max_line_bytes:
                    raise ProtocolError("app-server output line exceeded limit")
                try:
                    envelope = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError("app-server emitted malformed JSON") from exc
                self._handle_envelope(envelope)
        except asyncio.CancelledError:
            raise
        except (ProtocolError, ProcessExitedError) as exc:
            await self._record_terminal_error(exc)

    def _handle_envelope(self, envelope: object) -> None:
        if not isinstance(envelope, dict) or (
            "jsonrpc" in envelope and envelope["jsonrpc"] != "2.0"
        ):
            raise ProtocolError("invalid JSON-RPC envelope")

        has_id = "id" in envelope
        has_method = "method" in envelope
        if has_id and not has_method and ("result" in envelope or "error" in envelope):
            self._handle_response(envelope)
            return
        if has_method and ("result" in envelope or "error" in envelope):
            raise ProtocolError("server message cannot contain response fields")

        method = envelope.get("method")
        params = envelope.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ProtocolError("invalid server message envelope")
        if has_id:
            request_id = envelope["id"]
            if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
                raise ProtocolError("invalid server request id")
            try:
                self._requests.put_nowait(ServerRequest(request_id, method, params))
            except asyncio.QueueFull as exc:
                raise ProtocolError("server request queue capacity exceeded") from exc
        else:
            try:
                self._notifications.put_nowait(ServerNotification(method, params))
            except asyncio.QueueFull as exc:
                raise ProtocolError("server notification queue capacity exceeded") from exc

    def _handle_response(self, envelope: dict[str, Any]) -> None:
        request_id = envelope.get("id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            raise ProtocolError("invalid response id")
        has_result = "result" in envelope
        has_error = "error" in envelope
        if has_result == has_error:
            raise ProtocolError("response must contain exactly one of result or error")
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if has_error:
            error = envelope["error"]
            if not isinstance(error, dict):
                raise ProtocolError("invalid JSON-RPC error")
            code = error.get("code")
            message = error.get("message")
            if (
                not isinstance(code, int)
                or isinstance(code, bool)
                or not isinstance(message, str)
            ):
                raise ProtocolError("invalid JSON-RPC error")
            future.set_exception(ServerRpcError(code, message, error.get("data")))
        else:
            future.set_result(envelope["result"])

    async def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None
        buffered = bytearray()
        truncated = False
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    if buffered or truncated:
                        self._append_stderr_entry(buffered, truncated)
                    return
                parts = chunk.split(b"\n")
                for index, part in enumerate(parts):
                    remaining = _STDERR_ENTRY_BYTES - len(buffered)
                    if remaining > 0:
                        buffered.extend(part[:remaining])
                    if len(part) > remaining:
                        truncated = True
                    if index < len(parts) - 1:
                        self._append_stderr_entry(buffered, truncated)
                        buffered.clear()
                        truncated = False
        except asyncio.CancelledError:
            raise

    def _append_stderr_entry(self, buffered: bytearray, truncated: bool) -> None:
        text = bytes(buffered).decode("utf-8", errors="replace").rstrip("\r")
        redacted = redact_text(text)
        if truncated:
            redacted = f"{redacted} …[truncated]"
        self._stderr_tail.append(redacted)

    async def _wait_for_process(self) -> None:
        process = self._process
        assert process is not None
        try:
            returncode = await process.wait()
        except asyncio.CancelledError:
            raise
        await self._record_terminal_error(ProcessExitedError(returncode))

    async def _record_terminal_error(self, error: BaseException) -> None:
        async with self._lifecycle_lock:
            if self._explicit_close or self._lifecycle is _Lifecycle.CLOSED:
                return
            if self._terminal_error is None:
                self._terminal_error = error
                self._closing_error = error
                self._fail_pending(error)
            self._lifecycle = _Lifecycle.CLOSING
            self._ensure_cleanup_task_locked()

    def _ensure_cleanup_task_locked(self) -> asyncio.Task[None]:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup_coordinator(), name="codex-app-server-cleanup"
            )
            self._cleanup_task.add_done_callback(self._consume_task_exception)
        return self._cleanup_task

    async def _cleanup_coordinator(self) -> None:
        startup_task = self._startup_task
        current = asyncio.current_task()
        if (
            self._process is None
            and startup_task is not None
            and startup_task is not current
            and not startup_task.done()
        ):
            try:
                await asyncio.shield(startup_task)
            except BaseException:
                pass
        await self._cleanup_resources()
        if (
            startup_task is not None
            and startup_task is not current
            and not startup_task.done()
        ):
            try:
                await asyncio.shield(startup_task)
            except BaseException:
                pass
        await self._mark_closed()

    async def _cleanup_resources(self) -> None:
        current = asyncio.current_task()
        background_tasks = [self._stdout_task, self._stderr_task, self._wait_task]
        for task in background_tasks:
            if task is not None and task is not current and not task.done():
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in background_tasks
                if task is not None and task is not current
            ),
            return_exceptions=True,
        )

        process = self._process
        if process is not None:
            process.stdin.close()
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

            drain_tasks = tuple(self._drain_tasks)
            for task in drain_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*drain_tasks, return_exceptions=True)

            write_operations = tuple(self._write_operations)
            if write_operations:
                await asyncio.gather(*write_operations, return_exceptions=True)

            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), self._close_timeout)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            else:
                await process.wait()
            try:
                await asyncio.wait_for(
                    process.stdin.wait_closed(), self._close_timeout
                )
            except (
                AttributeError,
                BrokenPipeError,
                ConnectionResetError,
                TimeoutError,
            ):
                pass

        self._stdout_task = None
        self._stderr_task = None
        self._wait_task = None
        self._process = None

    async def _mark_closed(self) -> None:
        async with self._lifecycle_lock:
            self._lifecycle = _Lifecycle.CLOSED
            self._closed_event.set()

    def _fail_pending(self, error: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def _ensure_available(self) -> None:
        process_exit: ProcessExitedError | None = None
        async with self._lifecycle_lock:
            startup_call = asyncio.current_task() is self._startup_task
            if self._lifecycle is _Lifecycle.CLOSED:
                self._raise_terminal_error()
                raise RuntimeError("Codex app-server client is closed")
            if self._lifecycle is _Lifecycle.CLOSING:
                self._raise_terminal_error()
                raise RuntimeError("Codex app-server client is closed")
            if self._lifecycle is _Lifecycle.NEW or self._process is None:
                raise RuntimeError("Codex app-server client is not started")
            if self._lifecycle is _Lifecycle.STARTING and not startup_call:
                raise RuntimeError("Codex app-server client is still starting")
            if self._process.returncode is not None:
                process_exit = ProcessExitedError(self._process.returncode)
        if process_exit is not None:
            await self._record_terminal_error(process_exit)
            raise process_exit

    def _raise_terminal_error(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error

    def _closing_exception(self) -> BaseException:
        return self._closing_error or ProcessExitedError(
            self._returncode(), "client closed"
        )

    def _returncode(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _nested_id(result: object, key: str) -> str:
        if not isinstance(result, dict):
            raise ProtocolError(f"{key} response result must be an object")
        nested = result.get(key)
        if not isinstance(nested, dict) or not isinstance(nested.get("id"), str):
            raise ProtocolError(f"{key} response is missing {key}.id")
        return nested["id"]
