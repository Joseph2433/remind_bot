from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from lark_bot.codex_remote_probe import (
    ProbeResult,
    probe_remote_clients,
    run_local_probe,
)


Event = tuple[str, str, str, int | None]


@dataclass(frozen=True)
class ScriptedExchange:
    method: str
    request_id: int | None
    params: dict[str, object]
    response: dict[str, object] | None = None


def rpc(
    method: str,
    request_id: int,
    response: dict[str, object],
    params: dict[str, object] | None = None,
) -> ScriptedExchange:
    return ScriptedExchange(method, request_id, params or {}, response)


def notification(method: str) -> ScriptedExchange:
    return ScriptedExchange(method, None, {})


class FakeSocket:
    def __init__(
        self,
        name: str,
        script: list[ScriptedExchange],
        events: list[Event],
    ) -> None:
        self.name = name
        self.script = deque(script)
        self.events = events
        self.ready_responses: deque[tuple[str, dict[str, object]]] = deque()
        self.closed = False

    async def send(self, raw: str) -> None:
        if not self.script:
            raise AssertionError(f"{self.name} received an unexpected request: {raw}")

        expected = self.script.popleft()
        message = json.loads(raw)
        assert message.get("method") == expected.method
        if expected.request_id is None:
            assert "id" not in message
        else:
            assert message.get("id") == expected.request_id
        assert message.get("params") == expected.params
        self.events.append(
            (self.name, "send", expected.method, expected.request_id)
        )

        if expected.response is not None:
            assert expected.response.get("id") == expected.request_id
            self.ready_responses.append((expected.method, expected.response))

    async def recv(self) -> str:
        if not self.ready_responses:
            raise AssertionError(
                f"{self.name} recv called without a matching scripted response"
            )
        method, response = self.ready_responses.popleft()
        response_id = response.get("id")
        assert isinstance(response_id, int)
        self.events.append((self.name, "recv", method, response_id))
        return json.dumps(response)

    async def close(self) -> None:
        self.closed = True

    def assert_finished(self) -> None:
        assert not self.script
        assert not self.ready_responses


class FakeProcess:
    def __init__(self, *, wait_blocks_after_terminate: bool = False) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_blocks_after_terminate = wait_blocks_after_terminate
        self.wait_calls = 0
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
        self.wait_calls += 1
        await self._done.wait()
        return self.returncode or 0


def initialize_script(client_name: str) -> list[ScriptedExchange]:
    return [
        rpc(
            "initialize",
            1,
            {"id": 1, "result": {}},
            {
                "clientInfo": {
                    "name": client_name,
                    "title": "Lark Bot Probe",
                    "version": "0.1.0",
                }
            },
        ),
        notification("initialized"),
    ]


def test_probe_initializes_two_clients_resumes_and_rechecks_primary() -> None:
    async def scenario() -> None:
        events: list[Event] = []
        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [
                rpc(
                    "thread/list",
                    2,
                    {"id": 2, "result": {"data": [{"id": "thread-secret"}]}},
                ),
                rpc(
                    "thread/list",
                    3,
                    {"id": 3, "result": {"data": [{"id": "thread-secret"}]}},
                ),
            ],
            events,
        )
        secondary = FakeSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [
                rpc(
                    "thread/list",
                    2,
                    {"id": 2, "result": {"data": [{"id": "thread-secret"}]}},
                ),
                rpc(
                    "thread/resume",
                    3,
                    {"id": 3, "result": {"thread": {"id": "thread-secret"}}},
                    {"threadId": "thread-secret"},
                ),
            ],
            events,
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
        primary.assert_finished()
        secondary.assert_finished()
        assert events == [
            ("primary", "send", "initialize", 1),
            ("primary", "recv", "initialize", 1),
            ("primary", "send", "initialized", None),
            ("secondary", "send", "initialize", 1),
            ("secondary", "recv", "initialize", 1),
            ("secondary", "send", "initialized", None),
            ("primary", "send", "thread/list", 2),
            ("primary", "recv", "thread/list", 2),
            ("secondary", "send", "thread/list", 2),
            ("secondary", "recv", "thread/list", 2),
            ("secondary", "send", "thread/resume", 3),
            ("secondary", "recv", "thread/resume", 3),
            ("primary", "send", "thread/list", 3),
            ("primary", "recv", "thread/list", 3),
        ]
        assert "thread-secret" not in json.dumps(result.to_public_dict())

    asyncio.run(scenario())


def test_probe_without_saved_threads_still_proves_two_client_listing() -> None:
    async def scenario() -> None:
        events: list[Event] = []
        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [
                rpc("thread/list", 2, {"id": 2, "result": {"data": []}}),
                rpc("thread/list", 3, {"id": 3, "result": {"data": []}}),
            ],
            events,
        )
        secondary = FakeSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [rpc("thread/list", 2, {"id": 2, "result": {"data": []}})],
            events,
        )

        result = await probe_remote_clients(primary, secondary)

        assert result.multi_client is True
        assert result.both_initialized is True
        assert result.both_listed_threads is True
        assert result.resume_attempted is False
        assert result.resume_succeeded is False
        assert result.primary_survived is True
        primary.assert_finished()
        secondary.assert_finished()
        assert not any(method == "thread/resume" for _, _, method, _ in events)

    asyncio.run(scenario())


def test_probe_reports_rejected_resume_and_does_not_recheck_primary() -> None:
    async def scenario() -> None:
        events: list[Event] = []
        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [
                rpc(
                    "thread/list",
                    2,
                    {"id": 2, "result": {"data": [{"id": "thread-secret"}]}},
                )
            ],
            events,
        )
        secondary = FakeSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [
                rpc(
                    "thread/list",
                    2,
                    {"id": 2, "result": {"data": [{"id": "thread-secret"}]}},
                ),
                rpc(
                    "thread/resume",
                    3,
                    {
                        "id": 3,
                        "error": {
                            "code": -32000,
                            "message": "thread-secret ws://127.0.0.1:6123",
                        },
                    },
                    {"threadId": "thread-secret"},
                ),
            ],
            events,
        )

        result = await probe_remote_clients(primary, secondary)

        assert result.multi_client is True
        assert result.both_initialized is True
        assert result.both_listed_threads is True
        assert result.resume_attempted is True
        assert result.resume_succeeded is False
        assert result.primary_survived is False
        assert result.error_type == "RuntimeError"
        primary.assert_finished()
        secondary.assert_finished()
        assert ("secondary", "recv", "thread/resume", 3) in events
        assert ("primary", "send", "thread/list", 3) not in events
        public = json.dumps(result.to_public_dict())
        assert "thread-secret" not in public
        assert "6123" not in public

    asyncio.run(scenario())


def test_probe_preserves_flags_when_secondary_thread_list_fails() -> None:
    async def scenario() -> None:
        events: list[Event] = []
        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [rpc("thread/list", 2, {"id": 2, "result": {"data": []}})],
            events,
        )
        secondary = FakeSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [
                rpc(
                    "thread/list",
                    2,
                    {"id": 2, "error": {"code": -32000, "message": "failed"}},
                )
            ],
            events,
        )

        result = await probe_remote_clients(primary, secondary)

        assert result.multi_client is True
        assert result.both_initialized is True
        assert result.both_listed_threads is False
        assert result.resume_attempted is False
        assert result.resume_succeeded is False
        assert result.primary_survived is False
        assert result.error_type == "RuntimeError"
        primary.assert_finished()
        secondary.assert_finished()

    asyncio.run(scenario())


def test_local_probe_records_collaborators_closes_clients_and_reaps_process() -> None:
    async def scenario() -> None:
        endpoint = "ws://127.0.0.1:6123"
        events: list[Event] = []
        process = FakeProcess()
        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [
                rpc("thread/list", 2, {"id": 2, "result": {"data": []}}),
                rpc("thread/list", 3, {"id": 3, "result": {"data": []}}),
            ],
            events,
        )
        secondary = FakeSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [rpc("thread/list", 2, {"id": 2, "result": {"data": []}})],
            events,
        )
        sockets = iter([primary, secondary])
        process_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        listener_calls: list[tuple[str, FakeProcess]] = []
        connector_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def process_factory(*args: object, **kwargs: object) -> FakeProcess:
            process_calls.append((args, kwargs))
            return process

        async def wait_listener(value: str, child: FakeProcess) -> None:
            listener_calls.append((value, child))

        async def connector(*args: object, **kwargs: object) -> FakeSocket:
            connector_calls.append((args, kwargs))
            return next(sockets)

        result = await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=process_factory,
            connector=connector,
            wait_listener=wait_listener,
            endpoint_factory=lambda: endpoint,
            version_reader=lambda executable: "codex-cli test",
        )

        assert result == ProbeResult(
            multi_client=True,
            both_initialized=True,
            both_listed_threads=True,
            resume_attempted=False,
            resume_succeeded=False,
            primary_survived=True,
            error_type=None,
            codex_version="codex-cli test",
        )
        primary.assert_finished()
        secondary.assert_finished()
        assert process_calls == [
            (
                (
                    "C:/tools/codex.exe",
                    "app-server",
                    "--listen",
                    endpoint,
                ),
                {
                    "stdin": asyncio.subprocess.DEVNULL,
                    "stdout": asyncio.subprocess.DEVNULL,
                    "stderr": asyncio.subprocess.DEVNULL,
                },
            )
        ]
        assert listener_calls == [(endpoint, process)]
        assert connector_calls == [
            ((endpoint,), {"proxy": None, "open_timeout": 5.0}),
            ((endpoint,), {"proxy": None, "open_timeout": 5.0}),
        ]
        assert primary.closed and secondary.closed
        assert process.terminated and not process.killed
        assert process.wait_calls == 1

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
        assert process.wait_calls == 1

    asyncio.run(scenario())


def test_local_probe_closes_first_socket_when_second_connector_fails() -> None:
    async def scenario() -> None:
        endpoint = "ws://127.0.0.1:6123"
        process = FakeProcess()
        first = FakeSocket("primary", [], [])
        connector_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def connector(*args: object, **kwargs: object) -> FakeSocket:
            connector_calls.append((args, kwargs))
            if len(connector_calls) == 1:
                return first
            raise OSError("second connection failed")

        result = await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=lambda *args, **kwargs: asyncio.sleep(0, result=process),
            connector=connector,
            wait_listener=lambda value, child: asyncio.sleep(0),
            endpoint_factory=lambda: endpoint,
            version_reader=lambda executable: "codex-cli test",
        )

        assert result.error_type == "OSError"
        assert connector_calls == [
            ((endpoint,), {"proxy": None, "open_timeout": 5.0}),
            ((endpoint,), {"proxy": None, "open_timeout": 5.0}),
        ]
        assert first.closed
        assert process.terminated and not process.killed
        assert process.wait_calls == 1

    asyncio.run(scenario())


def test_local_probe_kills_and_reaps_process_when_termination_times_out() -> None:
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
        assert process.returncode == -9
        assert process.wait_calls == 2

    asyncio.run(scenario())
