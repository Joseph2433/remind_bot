from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypeVar

from websockets.asyncio.client import connect

from lark_bot.codex_interactive import _loopback_endpoint, _wait_for_listener


_T = TypeVar("_T")


class ProbeSocket(Protocol):
    """The small structural interface needed by the probe's WebSocket client."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProbeResult:
    multi_client: bool = False
    both_initialized: bool = False
    both_listed_threads: bool = False
    resume_attempted: bool = False
    resume_succeeded: bool = False
    primary_survived: bool = False
    error_type: str | None = None
    codex_version: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return asdict(self)


async def _rpc(
    socket: ProbeSocket,
    request_id: int,
    method: str,
    params: dict[str, object],
    *,
    timeout: float,
) -> dict[str, Any]:
    """Send one request and await its exactly-correlated response.

    App-server notifications and responses for other request IDs are ignored.  Error
    details are deliberately not propagated because they may contain endpoints,
    thread IDs, or authentication material.
    """

    async with asyncio.timeout(timeout):
        await socket.send(
            json.dumps(
                {"id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            )
        )
        while True:
            raw = await socket.recv()
            message = json.loads(raw)
            if not isinstance(message, dict):
                continue
            response_id = message.get("id")
            if "method" in message:
                valid_server_id = type(response_id) is int or isinstance(
                    response_id, str
                )
                if (
                    valid_server_id
                    and isinstance(message.get("method"), str)
                ):
                    await socket.send(
                        json.dumps(
                            {
                                "id": response_id,
                                "error": {
                                    "code": -32601,
                                    "message": "Method not supported",
                                },
                            },
                            separators=(",", ":"),
                        )
                    )
                continue
            if type(response_id) is not type(request_id) or response_id != request_id:
                continue
            has_result = "result" in message
            has_error = "error" in message
            if has_result == has_error:
                continue
            if has_error:
                raise RuntimeError("Codex app-server RPC failed")
            result = message.get("result")
            return result if isinstance(result, dict) else {}


async def _initialize(socket: ProbeSocket, name: str, *, timeout: float) -> None:
    await _rpc(
        socket,
        1,
        "initialize",
        {
            "clientInfo": {
                "name": name,
                "title": "Lark Bot Probe",
                "version": "0.1.0",
            }
        },
        timeout=timeout,
    )
    await socket.send(
        json.dumps({"method": "initialized", "params": {}}, separators=(",", ":"))
    )


def _first_thread_id(result: dict[str, Any]) -> str | None:
    for key in ("data", "items"):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            thread_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return None


async def _close_socket(socket: ProbeSocket, *, timeout: float) -> None:
    await asyncio.wait_for(socket.close(), timeout=timeout)


async def probe_remote_clients(
    primary: ProbeSocket,
    secondary: ProbeSocket,
    *,
    timeout: float = 5.0,
) -> ProbeResult:
    multi_client = False
    both_initialized = False
    both_listed_threads = False
    resume_attempted = False
    resume_succeeded = False
    primary_survived = False
    try:
        # Keep initialization sequential: this mirrors the protocol handshake and
        # makes partial progress unambiguous in the returned result.
        await _initialize(primary, "lark_bot_probe_primary", timeout=timeout)
        await _initialize(secondary, "lark_bot_probe_secondary", timeout=timeout)
        multi_client = True
        both_initialized = True

        try:
            primary_list = await _rpc(primary, 2, "thread/list", {}, timeout=timeout)
            secondary_list = await _rpc(
                secondary, 2, "thread/list", {}, timeout=timeout
            )
            both_listed_threads = True

            thread_id = _first_thread_id(secondary_list) or _first_thread_id(
                primary_list
            )
            if thread_id is not None:
                resume_attempted = True
                await _rpc(
                    secondary,
                    3,
                    "thread/resume",
                    {"threadId": thread_id},
                    timeout=timeout,
                )
                resume_succeeded = True
        finally:
            await _close_socket(secondary, timeout=timeout)

        # Recheck primary only after the optional resume succeeded, or when no
        # resume was needed.  Exceptions above intentionally skip this operation.
        await _rpc(primary, 3, "thread/list", {}, timeout=timeout)
        primary_survived = True
        return ProbeResult(
            multi_client=multi_client,
            both_initialized=both_initialized,
            both_listed_threads=both_listed_threads,
            resume_attempted=resume_attempted,
            resume_succeeded=resume_succeeded,
            primary_survived=primary_survived,
        )
    except Exception as error:
        return ProbeResult(
            multi_client=multi_client,
            both_initialized=both_initialized,
            both_listed_threads=both_listed_threads,
            resume_attempted=resume_attempted,
            resume_succeeded=resume_succeeded,
            primary_survived=primary_survived,
            error_type=type(error).__name__,
        )


def _default_version_reader(executable: str) -> str:
    completed = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    lines = completed.stdout.strip().splitlines()
    return lines[0][:200] if lines else "unknown"


async def _stop_process(process: Any, *, timeout: float) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            pass


def _schedule_late_cleanup(
    task: asyncio.Future[_T],
    cleanup: Callable[[_T], Awaitable[None]],
) -> None:
    def cleanup_when_done(done: asyncio.Future[_T]) -> None:
        try:
            resource = done.result()
        except BaseException:
            return
        loop = done.get_loop()
        if loop.is_closed():
            return
        cleanup_task = loop.create_task(cleanup(resource))

        def consume_cleanup_result(completed: asyncio.Task[None]) -> None:
            try:
                completed.result()
            except BaseException:
                pass

        cleanup_task.add_done_callback(consume_cleanup_result)

    task.add_done_callback(cleanup_when_done)


async def _acquire_resource(
    acquisition: Awaitable[_T],
    *,
    cleanup: Callable[[_T], Awaitable[None]],
    timeout: float,
) -> _T:
    task = asyncio.ensure_future(acquisition)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            resource = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if task not in done:
                _schedule_late_cleanup(task, cleanup)
                raise cancelled
            try:
                resource = task.result()
            except BaseException:
                raise cancelled
        except BaseException:
            raise cancelled
        try:
            await cleanup(resource)
        except Exception:
            pass
        raise cancelled


async def run_local_probe(
    codex_path: str = "codex",
    *,
    which: Callable[[str], str | None] = shutil.which,
    process_factory: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
    connector: Callable[..., Awaitable[ProbeSocket]] = connect,
    wait_listener: Callable[[str, Any], Awaitable[None]] = _wait_for_listener,
    endpoint_factory: Callable[[], str] = _loopback_endpoint,
    version_reader: Callable[[str], str] = _default_version_reader,
    timeout: float = 5.0,
    close_timeout: float = 2.0,
) -> ProbeResult:
    try:
        executable = which(codex_path)
    except Exception as error:
        return ProbeResult(error_type=type(error).__name__)
    if executable is None:
        return ProbeResult(error_type="FileNotFoundError")

    try:
        codex_version = version_reader(executable)
    except Exception:
        codex_version = "unknown"

    process: Any | None = None
    sockets: list[ProbeSocket] = []

    async def cleanup_process(acquired: Any) -> None:
        await _stop_process(acquired, timeout=close_timeout)

    async def cleanup_socket(acquired: ProbeSocket) -> None:
        try:
            await _close_socket(acquired, timeout=close_timeout)
        except Exception:
            pass

    try:
        endpoint = endpoint_factory()
        process = await _acquire_resource(
            process_factory(
                executable,
                "app-server",
                "--listen",
                endpoint,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            cleanup=cleanup_process,
            timeout=close_timeout,
        )
        await asyncio.wait_for(wait_listener(endpoint, process), timeout=timeout)
        primary = await _acquire_resource(
            connector(endpoint, proxy=None, open_timeout=timeout),
            cleanup=cleanup_socket,
            timeout=close_timeout,
        )
        sockets.append(primary)
        secondary = await _acquire_resource(
            connector(endpoint, proxy=None, open_timeout=timeout),
            cleanup=cleanup_socket,
            timeout=close_timeout,
        )
        sockets.append(secondary)
        result = await probe_remote_clients(primary, secondary, timeout=timeout)
        return ProbeResult(**{**result.to_public_dict(), "codex_version": codex_version})
    except Exception as error:
        return ProbeResult(error_type=type(error).__name__, codex_version=codex_version)
    finally:
        for socket in reversed(sockets):
            try:
                await _close_socket(socket, timeout=close_timeout)
            except Exception:
                pass
        if process is not None:
            await _stop_process(process, timeout=close_timeout)


def main() -> None:
    result = asyncio.run(run_local_probe())
    print(json.dumps(result.to_public_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
