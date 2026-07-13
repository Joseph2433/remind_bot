from __future__ import annotations

import asyncio
import json
from typing import Any

from lark_bot.codex_remote_probe import (
    ProbeResult,
    probe_remote_clients,
    run_local_probe,
)


class FakeSocket:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses: asyncio.Queue[str] = asyncio.Queue()
        for response in responses:
            self.responses.put_nowait(json.dumps(response))
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        return await self.responses.get()

    async def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, *, wait_blocks_after_terminate: bool = False) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_blocks_after_terminate = wait_blocks_after_terminate
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        if not self.wait_blocks_after_terminate:
            self.returncode = 0
            self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode or 0


def test_probe_initializes_two_clients_resumes_and_rechecks_primary() -> None:
    async def scenario() -> None:
        primary = FakeSocket(
            [
                {"id": 1, "result": {"userAgent": "codex-test"}},
                {"id": 2, "result": {"data": [{"id": "thread-secret"}]}},
                {"id": 3, "result": {"data": [{"id": "thread-secret"}]}},
            ]
        )
        secondary = FakeSocket(
            [
                {"id": 1, "result": {"userAgent": "codex-test"}},
                {"id": 2, "result": {"data": [{"id": "thread-secret"}]}},
                {"id": 3, "result": {"thread": {"id": "thread-secret"}}},
            ]
        )

        result = await probe_remote_clients(primary, secondary)

        assert result == ProbeResult(
            multi_client=True,
            both_initialized=True,
            both_listed_threads=True,
            resume_attempted=True,
            resume_succeeded=True,
            primary_survived=True,
            error_type=None,
        )
        assert [message.get("method") for message in primary.sent] == [
            "initialize",
            "initialized",
            "thread/list",
            "thread/list",
        ]
        assert [message.get("method") for message in secondary.sent] == [
            "initialize",
            "initialized",
            "thread/list",
            "thread/resume",
        ]
        assert "thread-secret" not in json.dumps(result.to_public_dict())

    asyncio.run(scenario())


def test_probe_without_saved_threads_still_proves_two_client_listing() -> None:
    async def scenario() -> None:
        primary = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": []}},
                {"id": 3, "result": {"data": []}},
            ]
        )
        secondary = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": []}},
            ]
        )

        result = await probe_remote_clients(primary, secondary)

        assert result.multi_client is True
        assert result.both_initialized is True
        assert result.both_listed_threads is True
        assert result.resume_attempted is False
        assert result.resume_succeeded is False
        assert result.primary_survived is True
        assert [message.get("method") for message in secondary.sent] == [
            "initialize",
            "initialized",
            "thread/list",
        ]

    asyncio.run(scenario())


def test_local_probe_closes_both_clients_and_terminates_process_on_success() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        primary = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": []}},
                {"id": 3, "result": {"data": []}},
            ]
        )
        secondary = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": []}},
            ]
        )
        sockets = iter([primary, secondary])

        result = await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=lambda *args, **kwargs: asyncio.sleep(0, result=process),
            connector=lambda *args, **kwargs: asyncio.sleep(0, result=next(sockets)),
            wait_listener=lambda endpoint, child: asyncio.sleep(0),
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            version_reader=lambda executable: "codex-cli test",
        )

        assert result.multi_client is True
        assert result.codex_version == "codex-cli test"
        assert primary.closed and secondary.closed
        assert process.terminated and not process.killed

    asyncio.run(scenario())


def test_local_probe_reports_connection_error_without_exposing_message_text() -> None:
    async def scenario() -> None:
        process = FakeProcess()

        async def fail_connect(*args: Any, **kwargs: Any) -> FakeSocket:
            raise OSError("ws://127.0.0.1:6123 token=secret")

        result = await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=lambda *args, **kwargs: asyncio.sleep(0, result=process),
            connector=fail_connect,
            wait_listener=lambda endpoint, child: asyncio.sleep(0),
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            version_reader=lambda executable: "codex-cli test",
        )

        public = json.dumps(result.to_public_dict())
        assert result.error_type == "OSError"
        assert "6123" not in public
        assert "secret" not in public
        assert process.terminated

    asyncio.run(scenario())


def test_local_probe_kills_process_when_graceful_termination_times_out() -> None:
    async def scenario() -> None:
        process = FakeProcess(wait_blocks_after_terminate=True)

        async def fail_connect(*args: Any, **kwargs: Any) -> FakeSocket:
            raise ConnectionError("unavailable")

        await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=lambda *args, **kwargs: asyncio.sleep(0, result=process),
            connector=fail_connect,
            wait_listener=lambda endpoint, child: asyncio.sleep(0),
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            version_reader=lambda executable: "codex-cli test",
            close_timeout=0.01,
        )

        assert process.terminated and process.killed

    asyncio.run(scenario())
