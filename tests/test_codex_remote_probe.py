from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

import pytest

from lark_bot.codex_remote_probe import (
    ProbeResult,
    _rpc,
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


def test_rpc_timeout_bounds_notification_flood() -> None:
    async def scenario() -> None:
        class NotificationFlood:
            async def send(self, raw: str) -> None:
                del raw

            async def recv(self) -> str:
                await asyncio.sleep(0.001)
                return json.dumps({"method": "notice", "params": {}})

            async def close(self) -> None:
                return None

        task = asyncio.create_task(
            _rpc(NotificationFlood(), 7, "thread/list", {}, timeout=0.01)
        )
        await asyncio.sleep(0.03)
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            pytest.fail("RPC timeout must cover the entire receive loop")
        with pytest.raises(asyncio.TimeoutError):
            task.result()

    asyncio.run(scenario())


def test_local_probe_reports_missing_executable_without_starting_anything() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def process_factory(*args: object, **kwargs: object) -> FakeProcess:
            del args, kwargs
            calls.append("process")
            return FakeProcess()

        async def connector(*args: object, **kwargs: object) -> FakeSocket:
            del args, kwargs
            calls.append("connector")
            raise AssertionError("connector must not run")

        result = await run_local_probe(
            which=lambda value: None,
            process_factory=process_factory,
            connector=connector,
            wait_listener=lambda endpoint, child: asyncio.sleep(0),
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            version_reader=lambda executable: "unreachable",
        )

        assert result.error_type == "FileNotFoundError"
        assert calls == []

    asyncio.run(scenario())


def test_local_probe_version_reader_failure_keeps_probe_result_and_reaps() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [
                rpc("thread/list", 2, {"id": 2, "result": {"data": []}}),
                rpc("thread/list", 3, {"id": 3, "result": {"data": []}}),
            ],
            [],
        )
        secondary = FakeSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [rpc("thread/list", 2, {"id": 2, "result": {"data": []}})],
            [],
        )
        sockets = iter([primary, secondary])

        async def connector(*args: object, **kwargs: object) -> FakeSocket:
            del args, kwargs
            return next(sockets)

        def version_reader(executable: str) -> str:
            del executable
            raise OSError("version unavailable")

        result = await run_local_probe(
            which=lambda value: "C:/tools/codex.exe",
            process_factory=lambda *args, **kwargs: asyncio.sleep(0, result=process),
            connector=connector,
            wait_listener=lambda endpoint, child: asyncio.sleep(0),
            endpoint_factory=lambda: "ws://127.0.0.1:6123",
            version_reader=version_reader,
        )

        assert result.error_type is None
        assert result.codex_version == "unknown"
        assert result.primary_survived is True
        assert primary.closed and secondary.closed
        assert process.terminated and process.wait_calls == 1

    asyncio.run(scenario())


def test_rpc_ignores_nonresponses_and_rejects_same_id_server_request() -> None:
    async def scenario() -> None:
        class QueuedSocket:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []
                self.responses = deque(
                    [
                        {"method": "notice", "params": {}},
                        {"id": 99, "result": {"wrong": True}},
                        {
                            "id": "server-42",
                            "method": "server/request",
                            "params": {},
                        },
                        {
                            "id": True,
                            "method": "server/request",
                            "params": {},
                        },
                        {
                            "id": 7,
                            "method": "server/request",
                            "params": {"token": "secret"},
                        },
                        {"id": 7},
                        {"id": 7, "result": {}, "error": {"code": -1}},
                        {"id": 7, "result": {"ok": True}},
                    ]
                )

            async def send(self, raw: str) -> None:
                self.sent.append(json.loads(raw))

            async def recv(self) -> str:
                return json.dumps(self.responses.popleft())

            async def close(self) -> None:
                return None

        socket = QueuedSocket()

        result = await _rpc(socket, 7, "thread/list", {}, timeout=0.1)

        assert result == {"ok": True}
        assert socket.sent == [
            {"id": 7, "method": "thread/list", "params": {}},
            {
                "id": "server-42",
                "error": {"code": -32601, "message": "Method not supported"},
            },
            {
                "id": 7,
                "error": {"code": -32601, "message": "Method not supported"},
            },
        ]
        assert "secret" not in json.dumps(socket.sent)

    asyncio.run(scenario())


def test_probe_closes_picker_before_rechecking_primary() -> None:
    async def scenario() -> None:
        events: list[Event] = []

        class OrderedSocket(FakeSocket):
            async def close(self) -> None:
                events.append((self.name, "close", "socket", None))
                await super().close()

        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [
                rpc("thread/list", 2, {"id": 2, "result": {"data": []}}),
                rpc("thread/list", 3, {"id": 3, "result": {"data": []}}),
            ],
            events,
        )
        secondary = OrderedSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [rpc("thread/list", 2, {"id": 2, "result": {"data": []}})],
            events,
        )

        result = await probe_remote_clients(primary, secondary)

        assert result.primary_survived is True
        close_index = events.index(("secondary", "close", "socket", None))
        recheck_index = events.index(("primary", "send", "thread/list", 3))
        assert close_index < recheck_index

    asyncio.run(scenario())


def test_probe_reports_primary_failure_after_picker_close_without_leakage() -> None:
    async def scenario() -> None:
        events: list[Event] = []

        class OrderedSocket(FakeSocket):
            async def close(self) -> None:
                events.append((self.name, "close", "socket", None))
                await super().close()

        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [
                rpc("thread/list", 2, {"id": 2, "result": {"data": []}}),
                rpc(
                    "thread/list",
                    3,
                    {
                        "id": 3,
                        "error": {
                            "code": -32000,
                            "message": "token=secret ws://127.0.0.1:6123",
                        },
                    },
                ),
            ],
            events,
        )
        secondary = OrderedSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [rpc("thread/list", 2, {"id": 2, "result": {"data": []}})],
            events,
        )

        result = await probe_remote_clients(primary, secondary)

        assert result.primary_survived is False
        assert result.error_type == "RuntimeError"
        close_index = events.index(("secondary", "close", "socket", None))
        recheck_index = events.index(("primary", "send", "thread/list", 3))
        assert close_index < recheck_index
        public = json.dumps(result.to_public_dict())
        assert "secret" not in public
        assert "6123" not in public

    asyncio.run(scenario())


def test_probe_does_not_recheck_primary_when_picker_close_fails() -> None:
    async def scenario() -> None:
        events: list[Event] = []

        class FailingCloseSocket(FakeSocket):
            async def close(self) -> None:
                raise OSError("token=secret ws://127.0.0.1:6123")

        primary = FakeSocket(
            "primary",
            initialize_script("lark_bot_probe_primary")
            + [rpc("thread/list", 2, {"id": 2, "result": {"data": []}})],
            events,
        )
        secondary = FailingCloseSocket(
            "secondary",
            initialize_script("lark_bot_probe_secondary")
            + [rpc("thread/list", 2, {"id": 2, "result": {"data": []}})],
            events,
        )

        result = await probe_remote_clients(primary, secondary)

        assert result.primary_survived is False
        assert result.error_type == "OSError"
        assert not any(
            name == "primary" and method == "thread/list" and request_id == 3
            for name, action, method, request_id in events
            if action == "send"
        )
        public = json.dumps(result.to_public_dict())
        assert "secret" not in public
        assert "6123" not in public

    asyncio.run(scenario())


def test_probe_cancellation_propagates() -> None:
    async def scenario() -> None:
        recv_started = asyncio.Event()

        class BlockingSocket:
            async def send(self, raw: str) -> None:
                del raw

            async def recv(self) -> str:
                recv_started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def close(self) -> None:
                return None

        task = asyncio.create_task(
            probe_remote_clients(BlockingSocket(), BlockingSocket())
        )
        await recv_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_local_probe_cancellation_propagates_after_cleanup() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        recv_started = asyncio.Event()

        class BlockingSocket:
            def __init__(self) -> None:
                self.closed = False

            async def send(self, raw: str) -> None:
                del raw

            async def recv(self) -> str:
                recv_started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def close(self) -> None:
                self.closed = True

        primary = BlockingSocket()
        secondary = BlockingSocket()
        sockets = iter([primary, secondary])

        async def connector(*args: object, **kwargs: object) -> BlockingSocket:
            del args, kwargs
            return next(sockets)

        task = asyncio.create_task(
            run_local_probe(
                which=lambda value: "C:/tools/codex.exe",
                process_factory=lambda *args, **kwargs: asyncio.sleep(
                    0, result=process
                ),
                connector=connector,
                wait_listener=lambda endpoint, child: asyncio.sleep(0),
                endpoint_factory=lambda: "ws://127.0.0.1:6123",
                version_reader=lambda executable: "codex-cli test",
            )
        )
        await recv_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert primary.closed and secondary.closed
        assert process.terminated and process.wait_calls == 1

    asyncio.run(scenario())


def test_local_probe_bounds_listener_wait_and_reaps_process() -> None:
    async def scenario() -> None:
        process = FakeProcess()

        async def wait_listener(endpoint: str, child: FakeProcess) -> None:
            del endpoint, child
            await asyncio.Event().wait()

        result = await asyncio.wait_for(
            run_local_probe(
                which=lambda value: "C:/tools/codex.exe",
                process_factory=lambda *args, **kwargs: asyncio.sleep(
                    0, result=process
                ),
                connector=lambda *args, **kwargs: asyncio.sleep(0),
                wait_listener=wait_listener,
                endpoint_factory=lambda: "ws://127.0.0.1:6123",
                version_reader=lambda executable: "codex-cli test",
                timeout=0.01,
                close_timeout=0.01,
            ),
            timeout=0.1,
        )

        assert result.error_type == "TimeoutError"
        assert process.terminated and process.wait_calls == 1

    asyncio.run(scenario())


def test_local_probe_bounds_socket_close_before_stopping_process() -> None:
    async def scenario() -> None:
        process = FakeProcess()

        class StuckCloseSocket:
            async def send(self, raw: str) -> None:
                del raw

            async def recv(self) -> str:
                raise AssertionError("unreachable")

            async def close(self) -> None:
                await asyncio.Event().wait()

        connector_calls = 0

        async def connector(*args: object, **kwargs: object) -> StuckCloseSocket:
            nonlocal connector_calls
            del args, kwargs
            connector_calls += 1
            if connector_calls == 1:
                return StuckCloseSocket()
            raise OSError("second connection failed")

        started = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(
            run_local_probe(
                which=lambda value: "C:/tools/codex.exe",
                process_factory=lambda *args, **kwargs: asyncio.sleep(
                    0, result=process
                ),
                connector=connector,
                wait_listener=lambda endpoint, child: asyncio.sleep(0),
                endpoint_factory=lambda: "ws://127.0.0.1:6123",
                version_reader=lambda executable: "codex-cli test",
                close_timeout=0.01,
            ),
            timeout=0.1,
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert result.error_type == "OSError"
        assert elapsed < 0.08
        assert process.terminated and process.wait_calls == 1

    asyncio.run(scenario())


def test_local_probe_bounds_post_kill_wait() -> None:
    async def scenario() -> None:
        class StuckProcess:
            returncode: int | None = None

            def __init__(self) -> None:
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> int:
                self.wait_calls += 1
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        process = StuckProcess()

        async def fail_connect(*args: object, **kwargs: object) -> FakeSocket:
            del args, kwargs
            raise ConnectionError("unavailable")

        result = await asyncio.wait_for(
            run_local_probe(
                which=lambda value: "C:/tools/codex.exe",
                process_factory=lambda *args, **kwargs: asyncio.sleep(
                    0, result=process
                ),
                connector=fail_connect,
                wait_listener=lambda endpoint, child: asyncio.sleep(0),
                endpoint_factory=lambda: "ws://127.0.0.1:6123",
                version_reader=lambda executable: "codex-cli test",
                close_timeout=0.01,
            ),
            timeout=0.1,
        )

        assert result.error_type == "ConnectionError"
        assert process.terminated and process.killed
        assert process.wait_calls == 2

    asyncio.run(scenario())
