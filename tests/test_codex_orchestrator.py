import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from lark_bot.codex.app_server import ServerNotification, ServerRequest
from lark_bot.codex.models import InteractionKind, InteractionStatus, SessionStatus
from lark_bot.codex_orchestrator import (
    CodexOrchestrator,
    OrchestratorEventType,
)
from lark_bot.storage.codex import SQLiteCodexStore


NOW = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


class IdFactory:
    def __init__(self, *values: str) -> None:
        self.values = deque(values)

    def __call__(self) -> str:
        return self.values.popleft()


class FakeAppServer:
    def __init__(self) -> None:
        self.requests: asyncio.Queue[ServerRequest] = asyncio.Queue()
        self.notifications: asyncio.Queue[ServerNotification] = asyncio.Queue()
        self.started = 0
        self.closed = 0
        self.thread_calls: list[tuple[str, str | None, str]] = []
        self.turn_calls: list[tuple[str, str]] = []
        self.interrupt_calls: list[tuple[str, str]] = []
        self.responses: list[tuple[int | str, object]] = []
        self.errors: list[tuple[int | str, int, str, object | None]] = []
        self.thread_id = "thread-1"
        self.turn_id = "turn-1"
        self.start_thread_error: BaseException | None = None
        self.start_turn_error: BaseException | None = None
        self.response_error: BaseException | None = None
        self.interrupt_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.respond_started = asyncio.Event()
        self.respond_release = asyncio.Event()
        self.block_respond = False
        self._closed = asyncio.Event()
        self._close_error: BaseException | None = None

    async def start(self) -> None:
        self.started += 1

    async def start_thread(
        self, cwd: str, model: str | None = None, sandbox: str = "workspace-write"
    ) -> str:
        self.thread_calls.append((cwd, model, sandbox))
        if self.start_thread_error is not None:
            raise self.start_thread_error
        return self.thread_id

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        self.turn_calls.append((thread_id, prompt))
        if self.start_turn_error is not None:
            raise self.start_turn_error
        return self.turn_id

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupt_calls.append((thread_id, turn_id))
        if self.interrupt_error is not None:
            raise self.interrupt_error

    async def respond(self, request_id: int | str, result: object) -> None:
        self.respond_started.set()
        if self.block_respond:
            await self.respond_release.wait()
        if self.response_error is not None:
            raise self.response_error
        self.responses.append((request_id, result))

    async def respond_error(
        self,
        request_id: int | str,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None:
        self.errors.append((request_id, code, message, data))

    async def wait_closed(self) -> None:
        await self._closed.wait()
        if self._close_error is not None:
            raise self._close_error

    async def close(self) -> None:
        self.closed += 1
        self._closed.set()
        if self.close_error is not None:
            raise self.close_error

    def die(self, error: BaseException | None = None) -> None:
        self._close_error = error
        self._closed.set()


def run(coro):
    return asyncio.run(coro)


def make_orchestrator(*ids: str, timeout: int = 1800):
    store = SQLiteCodexStore(":memory:")
    app = FakeAppServer()
    clock = Clock()
    orchestrator = CodexOrchestrator(
        store,
        app,
        now=clock,
        id_factory=IdFactory(*ids),
        interaction_timeout_seconds=timeout,
    )
    return orchestrator, store, app, clock


async def create_running_session(orchestrator: CodexOrchestrator):
    return await orchestrator.create_session(
        "task", "C:/workspace", "secret prompt", model="gpt", sandbox="read-only"
    )


def test_event_queue_capacity_must_be_positive():
    with pytest.raises(ValueError, match="capacity"):
        CodexOrchestrator(
            SQLiteCodexStore(":memory:"),
            FakeAppServer(),
            now=Clock(),
            id_factory=IdFactory(),
            event_queue_capacity=0,
        )


def test_create_session_persists_state_without_prompt_and_emits_started():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")

        session = await create_running_session(orchestrator)

        assert session.status is SessionStatus.RUNNING
        assert session.thread_id == "thread-1"
        assert session.turn_id == "turn-1"
        assert app.thread_calls == [("C:/workspace", "gpt", "read-only")]
        assert app.turn_calls == [("thread-1", "secret prompt")]
        persisted = store.get_session("session-1")
        assert store.list_due_outbox(now=NOW + timedelta(seconds=4), limit=10) == []
        delayed = store.list_due_outbox(now=NOW + timedelta(seconds=5), limit=10)
        assert len(delayed) == 1
        assert delayed[0].created_at == NOW
        assert delayed[0].next_attempt_at == NOW + timedelta(seconds=5)
        assert "secret prompt" not in persisted.model_dump_json()
        event = orchestrator.events.get_nowait()
        assert event.event_type is OrchestratorEventType.SESSION_STARTED
        assert event.status is SessionStatus.RUNNING
        assert "secret prompt" not in event.summary

    run(scenario())


def test_interactive_session_is_created_without_prompt_and_bound_to_external_thread():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")

        session = await orchestrator.create_interactive_session(
            "interactive", "C:/workspace", "gpt", "workspace-write"
        )

        assert session.status is SessionStatus.STARTING
        assert session.thread_id is None
        assert app.thread_calls == []
        assert app.turn_calls == []
        assert orchestrator.bind_interactive_thread(
            session.id, "external-thread", "external-turn"
        )
        bound = store.get_session(session.id)
        assert bound.status is SessionStatus.RUNNING
        assert bound.thread_id == "external-thread"
        assert bound.turn_id == "external-turn"
        assert not orchestrator.bind_interactive_thread(
            session.id, "different-thread"
        )

    run(scenario())


def test_close_interactive_session_does_not_interrupt_managed_app_server():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")
        session = await orchestrator.create_interactive_session(
            "interactive", "C:/workspace"
        )
        assert orchestrator.bind_interactive_thread(
            session.id, "external-thread", "external-turn"
        )

        assert await orchestrator.close_interactive_session(session.id)

        closed = store.get_session(session.id)
        assert closed.status is SessionStatus.CANCELLED
        assert app.interrupt_calls == []
        event = orchestrator.events.get_nowait()
        assert event.event_type is OrchestratorEventType.SESSION_COMPLETED

    run(scenario())


def test_interactive_session_accepts_new_approval_after_interrupted_turn():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator(
            "session-1", "interaction-1", "interaction-2"
        )
        session = await orchestrator.create_interactive_session(
            "interactive", "C:/workspace"
        )
        assert orchestrator.bind_interactive_thread(
            session.id, "external-thread", "turn-1"
        )
        first = await orchestrator.process_server_request(
            ServerRequest(
                "rpc-1",
                "item/commandExecution/requestApproval",
                {"threadId": "external-thread", "turnId": "turn-1"},
            ),
            session_id=session.id,
        )
        assert first is not None

        await orchestrator.process_notification(
            ServerNotification(
                "turn/completed",
                {
                    "threadId": "external-thread",
                    "turn": {"id": "turn-1", "status": "interrupted"},
                },
            )
        )

        finished = store.get_session(session.id)
        assert finished.status is SessionStatus.RUNNING
        assert finished.turn_id is None
        events = []
        while not orchestrator.events.empty():
            events.append(orchestrator.events.get_nowait())
        assert any(
            event.event_type.value == "turn_interrupted" for event in events
        )
        assert (
            store.get_interaction("interaction-1").status
            is InteractionStatus.CANCELLED
        )
        await orchestrator.process_notification(
            ServerNotification(
                "turn/started",
                {"threadId": "external-thread", "turn": {"id": "turn-2"}},
            )
        )
        stale = await orchestrator.process_server_request(
            ServerRequest(
                "rpc-stale",
                "item/commandExecution/requestApproval",
                {"threadId": "external-thread", "turnId": "turn-1"},
            ),
            session_id=session.id,
        )
        assert stale is None
        second = await orchestrator.process_server_request(
            ServerRequest(
                "rpc-2",
                "item/commandExecution/requestApproval",
                {"threadId": "external-thread", "turnId": "turn-2"},
            ),
            session_id=session.id,
        )

        assert second is not None
        assert second.id == "interaction-2"
        await orchestrator.process_notification(
            ServerNotification(
                "turn/completed",
                {
                    "threadId": "external-thread",
                    "turn": {"id": "turn-1", "status": "interrupted"},
                },
            )
        )
        current = store.get_session(session.id)
        assert current.status is SessionStatus.WAITING_FOR_APPROVAL
        assert current.turn_id == "turn-2"
        assert (
            store.get_interaction("interaction-2").status
            is InteractionStatus.PENDING
        )
        assert app.errors == [
            (
                "rpc-stale",
                -32602,
                "request belongs to an inactive turn",
                None,
            )
        ]

    run(scenario())


def test_create_session_failure_marks_failed_with_redacted_summary_and_reraises():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")
        app.start_thread_error = RuntimeError("token=server-secret " + "x" * 3000)

        with pytest.raises(RuntimeError, match="server-secret"):
            await create_running_session(orchestrator)

        session = store.get_session("session-1")
        assert session.status is SessionStatus.FAILED
        assert "server-secret" not in session.summary
        assert len(session.summary) <= 2000
        event = orchestrator.events.get_nowait()
        assert event.event_type is OrchestratorEventType.SESSION_COMPLETED
        assert event.status is SessionStatus.FAILED
        assert "server-secret" not in event.summary

    run(scenario())


def test_create_session_failure_never_persists_prompt_echoed_by_server():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")
        app.start_turn_error = RuntimeError("rejected secret prompt")

        with pytest.raises(RuntimeError, match="secret prompt"):
            await create_running_session(orchestrator)

        assert "secret prompt" not in store.get_session("session-1").summary
        assert "secret prompt" not in orchestrator.events.get_nowait().summary

    run(scenario())


@pytest.mark.parametrize(
    ("method", "kind", "waiting_status"),
    [
        ("item/commandExecution/requestApproval", InteractionKind.EXEC_APPROVAL, SessionStatus.WAITING_FOR_APPROVAL),
        ("item/fileChange/requestApproval", InteractionKind.FILE_CHANGE_APPROVAL, SessionStatus.WAITING_FOR_APPROVAL),
        ("item/permissions/requestApproval", InteractionKind.PERMISSION_REQUEST, SessionStatus.WAITING_FOR_APPROVAL),
        ("item/tool/requestUserInput", InteractionKind.USER_INPUT, SessionStatus.WAITING_FOR_INPUT),
    ],
)
def test_server_requests_create_redacted_pending_interactions(method, kind, waiting_status):
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1", "interaction-1")
        await create_running_session(orchestrator)
        orchestrator.events.get_nowait()
        params = {
            "threadId": "thread-1",
            "reason": "token=request-secret",
            "command": "echo token=command-secret",
            "path": "C:/password=path-secret",
            "questions": [{"id": "q1", "question": "api_key=question-secret?"}],
            "permissions": {"network": True},
        }

        interaction = await orchestrator.process_server_request(
            ServerRequest("rpc-1", method, params)
        )

        assert interaction.kind is kind
        assert interaction.status is InteractionStatus.PENDING
        assert interaction.expires_at == NOW + timedelta(minutes=30)
        assert all(secret not in interaction.payload_summary for secret in (
            "request-secret", "command-secret", "path-secret", "question-secret"
        ))
        assert store.get_session("session-1").status is waiting_status
        assert "questions" not in store.get_interaction("interaction-1").model_dump_json()
        event = orchestrator.events.get_nowait()
        assert event.event_type is OrchestratorEventType.INTERACTION_REQUESTED
        assert event.interaction_id == "interaction-1"
        assert app.responses == []

    run(scenario())


def test_unknown_and_invalid_server_requests_return_protocol_errors():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")
        await create_running_session(orchestrator)

        assert await orchestrator.process_server_request(
            ServerRequest(1, "unknown/method", {"threadId": "thread-1"})
        ) is None
        assert await orchestrator.process_server_request(
            ServerRequest(2, "item/commandExecution/requestApproval", {"threadId": "missing"})
        ) is None

        assert [error[1] for error in app.errors] == [-32601, -32602]
        assert store.get_interaction("interaction-1") is None

    run(scenario())


def test_numeric_and_string_request_ids_are_distinct_and_reusable_after_resolution():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator(
            "session-1", "numeric", "string", "reused"
        )
        await create_running_session(orchestrator)
        method = "item/commandExecution/requestApproval"
        params = {"threadId": "thread-1"}
        numeric = await orchestrator.process_server_request(ServerRequest(1, method, params))
        string = await orchestrator.process_server_request(ServerRequest("1", method, params))
        assert numeric.request_id == "1"
        assert string.request_id == '"1"'
        assert await orchestrator.resolve_interaction("numeric", "user", allow=True)
        reused = await orchestrator.process_server_request(ServerRequest(1, method, params))
        assert reused is not None
        assert app.errors == []

    run(scenario())


def test_remaining_user_input_keeps_session_waiting_after_approval_resolution():
    async def scenario():
        orchestrator, store, _, _ = make_orchestrator("session-1", "approval", "input")
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            1, "item/commandExecution/requestApproval", {"threadId": "thread-1"}
        ))
        await orchestrator.process_server_request(ServerRequest(
            2, "item/tool/requestUserInput",
            {"threadId": "thread-1", "questions": [{"id": "q", "question": "Value?"}]},
        ))

        assert await orchestrator.resolve_interaction("approval", "user", allow=True)
        assert store.get_session("session-1").status is SessionStatus.WAITING_FOR_INPUT

    run(scenario())


@pytest.mark.parametrize(
    ("method", "allow", "expected_decision", "expected_response"),
    [
        ("item/commandExecution/requestApproval", True, "approved", {"decision": "accept"}),
        ("item/commandExecution/requestApproval", False, "denied", {"decision": "decline"}),
        ("item/fileChange/requestApproval", True, "accept", {"decision": "accept"}),
        ("item/fileChange/requestApproval", False, "decline", {"decision": "decline"}),
        ("item/permissions/requestApproval", True, "granted", {"permissions": {"network": True}, "scope": "turn", "strictAutoReview": False}),
        ("item/permissions/requestApproval", False, "denied", {"permissions": {}, "scope": "turn", "strictAutoReview": False}),
    ],
)
def test_resolve_approval_first_wins_and_normalizes_decision(method, allow, expected_decision, expected_response):
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1", "interaction-1")
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-1", method, {"threadId": "thread-1", "permissions": {"network": True}}
        ))

        assert await orchestrator.resolve_interaction("interaction-1", "user-1", allow=allow)
        assert not await orchestrator.resolve_interaction("interaction-1", "user-2", allow=not allow)

        interaction = store.get_interaction("interaction-1")
        assert interaction.decision == expected_decision
        assert interaction.actor_id == "user-1"
        assert app.responses == [("rpc-1", expected_response)]
        assert store.get_session("session-1").status is SessionStatus.RUNNING

    run(scenario())


def test_user_input_validation_happens_before_claim_and_raw_answers_stay_memory_only():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1", "interaction-1")
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-input", "item/tool/requestUserInput",
            {"threadId": "thread-1", "questions": [{"id": "q1", "question": "Value?"}]},
        ))

        with pytest.raises(ValueError, match="missing answers"):
            await orchestrator.resolve_interaction("interaction-1", "user", answers={})
        assert store.get_interaction("interaction-1").status is InteractionStatus.PENDING

        assert await orchestrator.resolve_interaction(
            "interaction-1", "user", answers={"q1": "token=reply-secret"}
        )
        assert store.get_interaction("interaction-1").decision == "submitted"
        assert "reply-secret" not in store.get_interaction("interaction-1").model_dump_json()
        assert app.responses == [("rpc-input", {"answers": {"q1": {"answers": ["token=reply-secret"]}}})]

    run(scenario())


def test_user_input_question_ids_are_available_only_while_live():
    async def scenario():
        orchestrator, _, _, _ = make_orchestrator("session-1", "interaction-1")
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-input", "item/tool/requestUserInput",
            {"threadId": "thread-1", "questions": [{"id": "q1"}, {"id": "q2"}]},
        ))
        assert orchestrator.get_user_input_question_ids("interaction-1") == ("q1", "q2")
        assert orchestrator.get_user_input_question_ids("missing") == ()
        await orchestrator.resolve_interaction(
            "interaction-1", "user", answers={"q1": "a", "q2": "b"}
        )
        assert orchestrator.get_user_input_question_ids("interaction-1") == ()

    run(scenario())


def test_external_lark_resolution_uses_injected_responder_not_managed_app_server():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator(
            "session-1", "interaction-1"
        )
        session = await orchestrator.create_interactive_session(
            "interactive", "C:/workspace", None, "workspace-write"
        )
        assert orchestrator.bind_interactive_thread(session.id, "external-thread")
        responses = []

        async def respond(request_id, result):
            responses.append((request_id, result))

        await orchestrator.process_server_request(
            ServerRequest(
                "rpc-1",
                "item/commandExecution/requestApproval",
                {"command": "echo ok"},
            ),
            session_id=session.id,
            responder=respond,
        )

        assert await orchestrator.resolve_interaction(
            "interaction-1", "ou_lark_actor", allow=True
        )
        assert responses == [("rpc-1", {"decision": "accept"})]
        assert app.responses == []
        assert store.get_interaction("interaction-1").actor_id == "ou_lark_actor"

    run(scenario())


def test_external_request_errors_use_injected_responder_not_managed_app_server():
    async def scenario():
        orchestrator, _, app, _ = make_orchestrator(
            "session-1", "interaction-1", "duplicate"
        )
        session = await orchestrator.create_interactive_session(
            "interactive", "C:/workspace", None, "workspace-write"
        )
        assert orchestrator.bind_interactive_thread(session.id, "external-thread")
        errors = []

        async def respond(request_id, result=None, *, error=None):
            errors.append((request_id, result, error))

        assert await orchestrator.process_server_request(
            ServerRequest(
                "rpc-1",
                "item/commandExecution/requestApproval",
                {"command": "echo ok"},
            ),
            session_id=session.id,
            responder=respond,
        )
        assert await orchestrator.process_server_request(
            ServerRequest("unknown", "unknown/method", {}),
            session_id=session.id,
            responder=respond,
        ) is None
        assert await orchestrator.process_server_request(
            ServerRequest(
                "inactive",
                "item/commandExecution/requestApproval",
                {"command": "echo no"},
            ),
            session_id="missing",
            responder=respond,
        ) is None
        assert await orchestrator.process_server_request(
            ServerRequest(
                "rpc-1",
                "item/commandExecution/requestApproval",
                {"command": "echo duplicate"},
            ),
            session_id=session.id,
            responder=respond,
        ) is None

        assert [item[2]["code"] for item in errors] == [-32601, -32602, -32600]
        assert app.errors == []

    run(scenario())


def test_terminal_first_claims_by_rpc_id_and_forwards_raw_result_exactly_once():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator(
            "session-1", "interaction-1"
        )
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(
            ServerRequest(
                "rpc-1",
                "item/commandExecution/requestApproval",
                {"threadId": "thread-1"},
            )
        )
        raw = {"decision": "acceptForSession"}

        assert await orchestrator.resolve_terminal_request("rpc-1", raw)
        assert not await orchestrator.resolve_terminal_request("rpc-1", raw)
        assert not await orchestrator.resolve_interaction(
            "interaction-1", "late-lark", allow=False
        )

        stored = store.get_interaction("interaction-1")
        assert stored.actor_id == "terminal"
        assert stored.decision == "approved"
        assert app.responses == [("rpc-1", raw)]

    run(scenario())


def test_lark_first_suppresses_late_terminal_response():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator(
            "session-1", "interaction-1"
        )
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(
            ServerRequest(
                "rpc-1",
                "item/fileChange/requestApproval",
                {"threadId": "thread-1"},
            )
        )

        assert await orchestrator.resolve_interaction(
            "interaction-1", "ou_lark_actor", allow=False
        )
        assert not await orchestrator.resolve_terminal_request(
            "rpc-1", {"decision": "accept"}
        )
        assert store.get_interaction("interaction-1").actor_id == "ou_lark_actor"
        assert app.responses == [("rpc-1", {"decision": "decline"})]

    run(scenario())


def test_terminal_user_input_validates_shape_before_claim_and_forwards_raw_result():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator(
            "session-1", "interaction-1"
        )
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(
            ServerRequest(
                "rpc-input",
                "item/tool/requestUserInput",
                {
                    "threadId": "thread-1",
                    "questions": [{"id": "q1", "question": "Value?"}],
                },
            )
        )

        with pytest.raises(ValueError, match="answers"):
            await orchestrator.resolve_terminal_request(
                "rpc-input", {"answers": {"q1": "not-an-answer-object"}}
            )
        assert store.get_interaction("interaction-1").status is InteractionStatus.PENDING

        raw = {"answers": {"q1": {"answers": ["secret reply"]}}}
        assert await orchestrator.resolve_terminal_request("rpc-input", raw)
        stored = store.get_interaction("interaction-1")
        assert stored.actor_id == "terminal"
        assert stored.decision == "submitted"
        assert "secret reply" not in stored.model_dump_json()
        assert app.responses == [("rpc-input", raw)]

    run(scenario())


def test_terminal_unknown_request_is_not_forwarded():
    async def scenario():
        orchestrator, _, app, _ = make_orchestrator()

        assert not await orchestrator.resolve_terminal_request(
            "missing", {"decision": "accept"}
        )
        assert app.responses == []

    run(scenario())


def test_response_failure_after_claim_interrupts_session_once():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1", "interaction-1")
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-1", "item/commandExecution/requestApproval", {"threadId": "thread-1"}
        ))
        app.response_error = RuntimeError("write failed")

        with pytest.raises(RuntimeError, match="write failed"):
            await orchestrator.resolve_interaction("interaction-1", "user", allow=True)

        assert store.get_session("session-1").status is SessionStatus.INTERRUPTED
        events = []
        while not orchestrator.events.empty():
            events.append(orchestrator.events.get_nowait())
        assert sum(event.event_type is OrchestratorEventType.SESSION_INTERRUPTED for event in events) == 1
        assert not await orchestrator.resolve_interaction("interaction-1", "user", allow=True)

    run(scenario())


def test_completion_wins_while_interaction_response_is_in_flight():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1", "interaction-1")
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            1, "item/commandExecution/requestApproval", {"threadId": "thread-1"}
        ))
        app.block_respond = True
        resolving = asyncio.create_task(
            orchestrator.resolve_interaction("interaction-1", "user", allow=True)
        )
        await app.respond_started.wait()
        await orchestrator.process_notification(ServerNotification(
            "turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}
        ))
        app.respond_release.set()
        assert await resolving
        assert store.get_session("session-1").status is SessionStatus.SUCCEEDED
        events = []
        while not orchestrator.events.empty():
            events.append(orchestrator.events.get_nowait())
        terminal_index = next(i for i, event in enumerate(events) if event.status is SessionStatus.SUCCEEDED)
        assert not any(event.event_type is OrchestratorEventType.INTERACTION_RESOLVED for event in events[terminal_index + 1:])

    run(scenario())


@pytest.mark.parametrize(
    ("turn_status", "session_status", "event_type"),
    [
        ("completed", SessionStatus.SUCCEEDED, OrchestratorEventType.SESSION_COMPLETED),
        ("failed", SessionStatus.FAILED, OrchestratorEventType.SESSION_COMPLETED),
        ("interrupted", SessionStatus.INTERRUPTED, OrchestratorEventType.SESSION_INTERRUPTED),
    ],
)
def test_turn_notifications_bind_turn_and_complete_session(turn_status, session_status, event_type):
    async def scenario():
        orchestrator, store, _, _ = make_orchestrator("session-1", "interaction-1")
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-1", "item/commandExecution/requestApproval", {"threadId": "thread-1"}
        ))
        await orchestrator.process_notification(ServerNotification(
            "turn/started", {"threadId": "thread-1", "turn": {"id": "turn-new"}}
        ))
        assert store.get_session("session-1").turn_id == "turn-new"

        await orchestrator.process_notification(ServerNotification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-new", "status": turn_status, "error": {"message": "token=terminal-secret"}}},
        ))

        session = store.get_session("session-1")
        assert session.status is session_status
        assert "terminal-secret" not in session.summary
        assert store.get_interaction("interaction-1").status is InteractionStatus.CANCELLED
        terminal_events = []
        while not orchestrator.events.empty():
            event = orchestrator.events.get_nowait()
            if event.event_type in {OrchestratorEventType.SESSION_COMPLETED, OrchestratorEventType.SESSION_INTERRUPTED}:
                terminal_events.append(event)
        assert len(terminal_events) == 1
        assert terminal_events[0].event_type is event_type

    run(scenario())


def test_server_request_resolved_notification_cancels_pending_live_interaction():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator(
            "session-1", "interaction-1"
        )
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(
            ServerRequest(
                "rpc-1",
                "item/commandExecution/requestApproval",
                {"threadId": "thread-1"},
            )
        )

        await orchestrator.process_notification(
            ServerNotification(
                "serverRequest/resolved", {"requestId": "rpc-1"}
            )
        )

        assert store.get_interaction("interaction-1").status is InteractionStatus.CANCELLED
        assert store.get_session("session-1").status is SessionStatus.RUNNING
        assert not await orchestrator.resolve_interaction(
            "interaction-1", "late-lark", allow=True
        )
        assert not await orchestrator.resolve_terminal_request(
            "rpc-1", {"decision": "accept"}
        )
        assert app.responses == []

    run(scenario())


def test_turn_completion_uses_redacted_top_level_error_message_only():
    async def scenario():
        orchestrator, store, _, _ = make_orchestrator("session-1")
        await create_running_session(orchestrator)

        await orchestrator.process_notification(ServerNotification(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "failed"},
                "error": {"message": "token=top-level-secret", "debug": "do not persist"},
            },
        ))

        summary = store.get_session("session-1").summary
        assert "top-level-secret" not in summary
        assert "[REDACTED]" in summary
        assert "do not persist" not in summary

    run(scenario())


def test_real_agent_message_final_answer_is_used_for_completion_summary():
    async def scenario():
        orchestrator, store, _, _ = make_orchestrator("session-1")
        await create_running_session(orchestrator)
        await orchestrator.process_notification(ServerNotification(
            "item/completed",
            {"threadId": "thread-1", "turnId": "turn-1", "item": {"type": "agentMessage", "phase": "commentary", "text": "draft"}},
        ))
        await orchestrator.process_notification(ServerNotification(
            "item/completed",
            {"threadId": "thread-1", "turnId": "turn-1", "item": {"type": "agentMessage", "phase": "final_answer", "text": "token=final-secret done"}},
        ))
        await orchestrator.process_notification(ServerNotification(
            "turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        ))
        summary = store.get_session("session-1").summary
        assert summary == "token=[REDACTED] done"

    run(scenario())


def test_expire_due_approval_denies_and_user_input_interrupts():
    async def scenario():
        orchestrator, store, app, clock = make_orchestrator(
            "session-approval", "approval", "session-input", "input", timeout=10
        )
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-approval", "item/commandExecution/requestApproval", {"threadId": "thread-1"}
        ))
        app.thread_id = "thread-2"
        app.turn_id = "turn-2"
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-input", "item/tool/requestUserInput",
            {"threadId": "thread-2", "questions": [{"id": "q", "question": "Value?"}]},
        ))
        clock.value += timedelta(seconds=11)

        expired = await orchestrator.expire_due_interactions()

        assert expired == ["approval", "input"]
        assert ("rpc-approval", {"decision": "decline"}) in app.responses
        assert app.interrupt_calls == [("thread-2", "turn-2")]
        assert store.get_session("session-approval").status is SessionStatus.RUNNING
        assert store.get_session("session-input").status is SessionStatus.INTERRUPTED
        assert await orchestrator.expire_due_interactions() == []

    run(scenario())


def test_expired_user_input_is_marked_interrupted_even_if_interrupt_rpc_fails():
    async def scenario():
        orchestrator, store, app, clock = make_orchestrator(
            "session-1", "interaction-1", timeout=10
        )
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-input", "item/tool/requestUserInput",
            {"threadId": "thread-1", "questions": [{"id": "q", "question": "Value?"}]},
        ))
        app.interrupt_error = RuntimeError("interrupt failed")
        clock.value += timedelta(seconds=11)

        with pytest.raises(RuntimeError, match="interrupt failed"):
            await orchestrator.expire_due_interactions()

        assert store.get_interaction("interaction-1").status is InteractionStatus.EXPIRED
        assert store.get_session("session-1").status is SessionStatus.INTERRUPTED

    run(scenario())


def test_start_reconciles_before_consumers_and_is_idempotent():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator()
        from lark_bot.codex.models import CodexSession
        store.create_session(CodexSession(
            id="old", name="old", cwd="C:/old", sandbox="workspace-write",
            status=SessionStatus.RUNNING, created_at=NOW, updated_at=NOW,
        ))

        await orchestrator.start()
        await orchestrator.start()

        assert app.started == 1
        assert store.get_session("old").status is SessionStatus.INTERRUPTED
        event = orchestrator.events.get_nowait()
        assert event.event_type is OrchestratorEventType.SESSION_INTERRUPTED
        await orchestrator.close()

    run(scenario())


def test_startup_reconciliation_never_blocks_when_hint_queue_is_full():
    async def scenario():
        store = SQLiteCodexStore(":memory:")
        app = FakeAppServer()
        from lark_bot.codex.models import CodexSession
        for session_id in ("old-1", "old-2"):
            store.create_session(CodexSession(id=session_id, name="old", cwd="C:/old", sandbox="workspace-write", status=SessionStatus.RUNNING, created_at=NOW, updated_at=NOW))
        orchestrator = CodexOrchestrator(store, app, now=Clock(), id_factory=IdFactory(), event_queue_capacity=1)
        await asyncio.wait_for(orchestrator.start(), timeout=1)
        assert store.list_due_outbox(now=NOW, limit=10) == []
        assert len(store.list_due_outbox(now=NOW + timedelta(seconds=5), limit=10)) == 2
        await orchestrator.close()

    run(scenario())


def test_consumers_process_queued_items_and_unexpected_death_interrupts_active_once():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1", "interaction-1")
        await orchestrator.start()
        await create_running_session(orchestrator)
        await app.requests.put(ServerRequest(
            "rpc-1", "item/commandExecution/requestApproval", {"threadId": "thread-1"}
        ))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert store.get_interaction("interaction-1") is not None

        app.die(RuntimeError("process died"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert store.get_session("session-1").status is SessionStatus.INTERRUPTED
        events = []
        while not orchestrator.events.empty():
            events.append(orchestrator.events.get_nowait())
        assert sum(event.event_type is OrchestratorEventType.SESSION_INTERRUPTED for event in events) == 1
        await orchestrator.close()

    run(scenario())


def test_duplicate_pending_request_is_rejected_and_consumer_survives():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1", "first", "duplicate", "next")
        await orchestrator.start()
        await create_running_session(orchestrator)
        method = "item/commandExecution/requestApproval"
        params = {"threadId": "thread-1"}
        await app.requests.put(ServerRequest(1, method, params))
        await app.requests.put(ServerRequest(1, method, params))
        await app.requests.put(ServerRequest(2, method, params))
        for _ in range(20):
            if store.get_interaction("next") is not None:
                break
            await asyncio.sleep(0)
        assert store.get_interaction("next") is not None
        assert any(error[1] == -32600 for error in app.errors)
        assert orchestrator.terminal_error is None
        await orchestrator.close()

    run(scenario())


def test_consumer_failure_is_observable_and_interrupts_active_sessions():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")
        await orchestrator.start()
        await create_running_session(orchestrator)
        await app.requests.put(ServerRequest(
            1, "item/commandExecution/requestApproval", {"threadId": "thread-1"}
        ))
        for _ in range(20):
            if orchestrator.terminal_error is not None:
                break
            await asyncio.sleep(0)
        assert isinstance(orchestrator.terminal_error, IndexError)
        assert store.get_session("session-1").status is SessionStatus.INTERRUPTED

    run(scenario())


def test_cancel_session_interrupts_turn_cancels_interactions_and_terminal_is_false():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1", "interaction-1")
        await create_running_session(orchestrator)
        await orchestrator.process_server_request(ServerRequest(
            "rpc-1", "item/commandExecution/requestApproval", {"threadId": "thread-1"}
        ))

        assert await orchestrator.cancel_session("session-1")
        assert not await orchestrator.cancel_session("session-1")
        assert not await orchestrator.cancel_session("missing")
        assert app.interrupt_calls == [("thread-1", "turn-1")]
        assert store.get_session("session-1").status is SessionStatus.CANCELLED
        assert store.get_interaction("interaction-1").status is InteractionStatus.CANCELLED

    run(scenario())


def test_cancel_remains_terminal_when_interrupt_rpc_fails():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")
        await create_running_session(orchestrator)
        app.interrupt_error = RuntimeError("interrupt failed")
        assert await orchestrator.cancel_session("session-1")
        assert store.get_session("session-1").status is SessionStatus.CANCELLED
        assert isinstance(orchestrator.terminal_error, RuntimeError)

    run(scenario())


def test_explicit_close_is_idempotent_and_does_not_interrupt_sessions():
    async def scenario():
        orchestrator, store, app, _ = make_orchestrator("session-1")
        await orchestrator.start()
        await create_running_session(orchestrator)

        await orchestrator.close()
        await orchestrator.close()

        assert app.closed == 1
        assert store.get_session("session-1").status is SessionStatus.RUNNING
        events = []
        while not orchestrator.events.empty():
            events.append(orchestrator.events.get_nowait())
        assert not any(event.event_type is OrchestratorEventType.SESSION_INTERRUPTED for event in events)

    run(scenario())


def test_close_failure_is_observable_and_still_marks_orchestrator_closed():
    async def scenario():
        orchestrator, _, app, _ = make_orchestrator()
        await orchestrator.start()
        app.close_error = RuntimeError("close failed")
        with pytest.raises(RuntimeError, match="close failed"):
            await orchestrator.close()
        assert orchestrator.terminal_error is app.close_error
        await orchestrator.close()
        assert app.closed == 1

    run(scenario())
