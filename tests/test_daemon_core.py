from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from lark_bot.codex.models import CodexSession, NotificationOutboxItem, SessionStatus
from lark_bot.server.daemon import DaemonRuntime, create_daemon_app, ensure_daemon_token


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self) -> None:
        self.sessions = {}
        self.items = []
        self.attached = []
        self.sent = []
        self.failures = []
        self.closed = False
        self.events = set()
        self.outbox_sent = asyncio.Event()

    def get_session(self, session_id): return self.sessions.get(session_id)
    def list_sessions(self, status=None): return [s for s in self.sessions.values() if status is None or s.status is status]
    def record_event_once(self, event_id):
        if event_id in self.events: return False
        self.events.add(event_id); return True
    def enqueue_outbox(self, **kwargs):
        created_at = kwargs.pop("created_at", datetime.now(timezone.utc))
        next_attempt_at = kwargs.pop("next_attempt_at", created_at)
        self.items.append(NotificationOutboxItem(id=len(self.items)+1, attempt_count=0, next_attempt_at=next_attempt_at, created_at=created_at, **kwargs)); return len(self.items)
    def list_due_outbox(self, *, now, **kwargs): return [item for item in self.items if item.next_attempt_at <= now and item.id not in self.sent and item.id not in {failure[0] for failure in self.failures}]
    def attach_lark_message_id(self, interaction_id, message_id): self.attached.append((interaction_id, message_id)); return True
    def mark_outbox_sent(self, outbox_id, **kwargs):
        self.sent.append(outbox_id)
        self.outbox_sent.set()
        return True
    def record_outbox_failure(self, outbox_id, **kwargs): self.failures.append((outbox_id, kwargs)); return True
    def close(self): self.closed = True


class FakeOrchestrator:
    def __init__(self, store):
        self.store, self.events, self.started, self.closed = store, asyncio.Queue(), False, False
        self.expiry_calls = 0
        self.expiry_called = asyncio.Event()
    async def start(self): self.started = True
    async def create_session(self, name, cwd, prompt, model=None, sandbox="workspace-write"):
        session = CodexSession(id="s1", name=name, cwd=cwd, model=model, sandbox=sandbox, status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        self.store.sessions[session.id] = session; return session
    async def cancel_session(self, session_id): return session_id == "s1"
    async def expire_due_interactions(self):
        self.expiry_calls += 1
        self.expiry_called.set()
        return []
    async def close(self): self.closed = True


class FakeLongConnection:
    def __init__(self): self.events, self.terminal_error, self.started, self.closed = asyncio.Queue(), None, False, False
    async def start(self): self.started = True
    async def close(self): self.closed = True


class FakeLark:
    def __init__(self):
        self.messages, self.rendered, self.closed = [], [], False

    def send_text(self, text):
        self.messages.append(text)
        return "m1"

    def send_rendered(self, message):
        self.rendered.append(message)
        # Keep a plain-text view for assertions that scan notification copy.
        if getattr(message, "msg_type", None) == "text":
            text = message.content.get("text", "")
        else:
            elements = message.content.get("body", {}).get("elements", [])
            text = "\n".join(
                str(element.get("content", ""))
                for element in elements
                if isinstance(element, dict)
            )
            header = message.content.get("header", {}).get("title", {})
            title = header.get("content", "") if isinstance(header, dict) else ""
            text = f"{title}\n{text}"
        self.messages.append(text)
        return "m1"

    def close(self): self.closed = True


def _runtime(tmp_path: Path, interactive_manager=None):
    store = FakeStore(); orchestrator = FakeOrchestrator(store); connection = FakeLongConnection(); lark = FakeLark()
    settings = SimpleNamespace(
        outbox_poll_seconds=0.01,
        interaction_expiry_poll_seconds=0.01,
        notification_delay_seconds=0.0,
        daemon_token_path=tmp_path / "token",
        message_format="card",
    )
    runtime = DaemonRuntime(settings, store, orchestrator, lark, connection, SimpleNamespace(route=lambda event: None), interactive_manager=interactive_manager, now=lambda: NOW)
    return runtime, store, orchestrator, connection, lark


class FakeInteractiveManager:
    def __init__(self):
        self.started = False
        self.closed = False
        self.created = []
        self.deleted = []

    async def start(self): self.started = True
    async def create_session(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(session_id="interactive-1", endpoint="ws://127.0.0.1:4567", remote_auth_token="remote-secret")
    async def close_session(self, session_id): self.deleted.append(session_id); return True
    async def close(self): self.closed = True


def test_token_is_created_once(workspace_tmp_path):
    path = workspace_tmp_path / ".lark-bot" / "daemon.token"
    first = ensure_daemon_token(path)
    assert ensure_daemon_token(path) == first
    assert path.read_text(encoding="utf-8").strip() == first


def test_api_requires_bearer_and_excludes_prompt(workspace_tmp_path):
    runtime, *_ = _runtime(workspace_tmp_path)
    with TestClient(create_daemon_app(runtime, token="secret")) as client:
        assert client.get("/api/v1/codex/sessions").status_code == 401
        response = client.post("/api/v1/codex/sessions", headers={"Authorization": "Bearer secret"}, json={"name": "n", "cwd": ".", "prompt": "TOP SECRET"})
        assert response.status_code == 201 and "TOP SECRET" not in response.text


def test_interactive_api_is_authenticated_and_returns_remote_secret_only_to_caller(workspace_tmp_path):
    manager = FakeInteractiveManager()
    runtime, *_ = _runtime(workspace_tmp_path, manager)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_daemon_app(runtime, token="secret")) as client:
        assert client.post("/api/v1/codex/interactive-sessions", json={"cwd": "."}).status_code == 401
        response = client.post(
            "/api/v1/codex/interactive-sessions",
            headers=headers,
            json={"name": "interactive", "cwd": "C:/work", "model": "gpt", "sandbox": "workspace-write"},
        )
        assert response.status_code == 201
        assert response.json() == {
            "session_id": "interactive-1",
            "endpoint": "ws://127.0.0.1:4567",
            "remote_auth_token": "remote-secret",
        }
        assert manager.created == [{"name": "interactive", "cwd": "C:/work", "model": "gpt", "sandbox": "workspace-write"}]
        assert client.delete("/api/v1/codex/interactive-sessions/interactive-1", headers=headers).status_code == 204
        assert manager.deleted == ["interactive-1"]
    assert manager.started and manager.closed


def test_interactive_api_reports_unconfigured_manager(workspace_tmp_path):
    runtime, *_ = _runtime(workspace_tmp_path)
    with TestClient(create_daemon_app(runtime, token="secret")) as client:
        response = client.post(
            "/api/v1/codex/interactive-sessions",
            headers={"Authorization": "Bearer secret"},
            json={"cwd": "."},
        )
        assert response.status_code == 503
        assert "interactive" in response.json()["detail"].lower()


def test_hook_endpoint_bounds_and_deduplicates(workspace_tmp_path):
    runtime, store, _, _, lark = _runtime(workspace_tmp_path); headers = {"Authorization": "Bearer secret"}
    with TestClient(create_daemon_app(runtime, token="secret")) as client:
        payload = {"hook_event_name": "Stop", "event_id": "e1", "output": "secret output"}
        assert client.post("/api/v1/codex/hooks", headers=headers, json=payload).status_code == 202
        assert client.post("/api/v1/codex/hooks", headers=headers, json=payload).status_code == 202
        assert len(store.items) == 1 and "secret output" not in store.items[0].payload_summary
        assert store.items[0].created_at == NOW
        assert store.items[0].next_attempt_at == NOW
        assert lark.messages == ["hook:Stop\nCodex hook Stop"]
        assert client.post("/api/v1/codex/hooks", headers=headers, content=b"x" * 65537).status_code == 413


def test_spool_hook_is_available_immediately(workspace_tmp_path):
    runtime, store, *_ = _runtime(workspace_tmp_path)
    spool = workspace_tmp_path / "spool"
    spool.mkdir()
    (spool / "hook-1.json").write_text(
        '{"hook_event_name":"Stop","event_id":"spool-1"}',
        encoding="utf-8",
    )

    runtime._drain_spool()

    assert store.items[0].created_at == NOW
    assert store.items[0].next_attempt_at == NOW


def test_runtime_sends_outbox_and_attaches_interaction(workspace_tmp_path):
    async def scenario():
        runtime, store, orchestrator, connection, lark = _runtime(workspace_tmp_path)
        store.enqueue_outbox(
            notification_type="orchestrator:interaction_requested",
            payload_summary="approve command",
            session_id="s1",
            interaction_id="i1",
            created_at=NOW,
            next_attempt_at=NOW,
        )
        await runtime.start()
        try:
            await asyncio.wait_for(store.outbox_sent.wait(), timeout=1)
        finally:
            await runtime.close()
        assert orchestrator.started and orchestrator.closed and connection.started and connection.closed
        assert store.attached == [("i1", "m1")] and store.sent == [1]
        assert any(
            "Codex 请求审批" in text
            and "请长按本消息并选择“回复”" in text
            and "yes 或 y 表示允许" in text
            and "no 或 n 表示拒绝" in text
            for text in lark.messages
        )
        assert store.closed and lark.closed
    asyncio.run(scenario())


def test_runtime_renders_session_identity(workspace_tmp_path):
    runtime, store, _, _, _ = _runtime(workspace_tmp_path)
    store.sessions["session-1"] = CodexSession(
        id="session-1",
        name="build",
        cwd=".",
        sandbox="workspace-write",
        status=SessionStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
    )

    rendered = runtime._render(
        SimpleNamespace(
            notification_type="orchestrator:turn_completed",
            payload_summary="done",
            interaction_id=None,
            session_id="session-1",
            agent="codex",
            session_name="build",
        )
    )

    assert "codex / build [session-]" in rendered.content["body"]["elements"][0]["content"]


def test_runtime_renders_interactive_turn_notifications_in_chinese(workspace_tmp_path):
    runtime, _, _, _, _ = _runtime(workspace_tmp_path)

    completed = runtime._render(
        SimpleNamespace(
            notification_type="orchestrator:turn_completed",
            payload_summary="done",
            interaction_id=None,
        )
    )
    interrupted = runtime._render(
        SimpleNamespace(
            notification_type="orchestrator:turn_interrupted",
            payload_summary="stopped",
            interaction_id=None,
        )
    )

    assert completed.msg_type == "interactive"
    assert completed.content["header"]["title"]["content"] == "Codex 本轮已完成"
    assert completed.content["body"]["elements"][0]["content"] == "done"
    assert interrupted.content["header"]["title"]["content"] == "Codex 本轮已中断"
    assert interrupted.content["body"]["elements"][0]["content"] == "stopped"


def test_runtime_runs_one_expiry_loop_and_collects_it_on_close(workspace_tmp_path):
    async def scenario():
        runtime, store, orchestrator, _, _ = _runtime(workspace_tmp_path)
        await runtime.start()
        await asyncio.wait_for(orchestrator.expiry_called.wait(), timeout=0.2)
        assert sum(task.get_name() == "interaction-expiry" for task in runtime._tasks) == 1
        await runtime.close()
        calls_after_close = orchestrator.expiry_calls
        await asyncio.sleep(0.03)
        assert orchestrator.expiry_calls == calls_after_close
        assert all(task.done() for task in runtime._tasks)

    asyncio.run(scenario())


def test_runtime_retries_expiry_after_safe_degradation(workspace_tmp_path):
    async def scenario():
        runtime, store, orchestrator, _, _ = _runtime(workspace_tmp_path)
        original = orchestrator.expire_due_interactions

        async def fail_once():
            if orchestrator.expiry_calls == 0:
                orchestrator.expiry_calls += 1
                raise RuntimeError("sensitive detail")
            return await original()

        orchestrator.expire_due_interactions = fail_once
        await runtime.start()
        await asyncio.wait_for(orchestrator.expiry_called.wait(), timeout=0.2)
        assert orchestrator.expiry_calls >= 2
        assert runtime.degraded_reason == "Interaction expiry unavailable (RuntimeError)"
        assert "sensitive detail" not in runtime.degraded_reason
        assert any(
            item.notification_type == "runtime:degraded"
            and item.payload_summary == "Interaction expiry unavailable (RuntimeError)"
            and item.next_attempt_at == NOW
            for item in store.items
        )
        await runtime.close()

    asyncio.run(scenario())


def test_outbox_retry_is_scheduled_from_failure_time(workspace_tmp_path):
    async def scenario():
        runtime, store, _, _, lark = _runtime(workspace_tmp_path)
        store.enqueue_outbox(
            notification_type="hook:Stop",
            payload_summary="Codex hook Stop",
            created_at=NOW - timedelta(minutes=1),
            next_attempt_at=NOW,
        )

        def fail(_message):
            raise RuntimeError("network detail")

        lark.send_rendered = fail
        worker = asyncio.create_task(runtime._outbox_worker())
        await asyncio.sleep(0.03)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        assert store.failures == [
            (
                1,
                {
                    "error": "send failed (RuntimeError)",
                    "next_attempt_at": NOW + timedelta(seconds=0.02),
                },
            )
        ]

    asyncio.run(scenario())
