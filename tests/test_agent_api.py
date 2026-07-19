from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import ValidationError

from lark_bot.modules.agent.agent_model import AgentKind, AgentSession, SessionStatus
from lark_bot.modules.agent.agent_service import AgentRegistry
from lark_bot.server.daemon import DaemonRuntime, create_daemon_app
from lark_bot.server.daemon.app import AgentSessionCreate


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self) -> None:
        self.sessions: dict[str, AgentSession] = {}
        self.events: set[str] = set()
        self.items: list[dict[str, object]] = []
        self.closed = False

    def get_session(self, session_id: str):
        return self.sessions.get(session_id)

    def list_sessions(self, status=None):
        return [s for s in self.sessions.values() if status is None or s.status is status]

    def record_event_once(self, event_id: str, **kwargs):
        if event_id in self.events:
            return False
        self.events.add(event_id)
        return True

    def enqueue_outbox(self, **kwargs):
        self.items.append(kwargs)
        return len(self.items)

    def close(self):
        self.closed = True


class FakeAdapter:
    def __init__(self, agent: AgentKind):
        self.agent = agent
        self.sessions: dict[str, AgentSession] = {}
        self.created: list[dict[str, object]] = []
        self.cancelled: list[str] = []
        self.expiry_calls = 0

    async def start(self):
        return None

    async def close(self):
        return None

    async def create_session(self, name, cwd, prompt, **options):
        self.created.append({"name": name, "cwd": cwd, "prompt": prompt, **options})
        session = AgentSession(
            session_id=f"{self.agent.value}-1",
            agent=self.agent,
            name=name,
            cwd=cwd,
            model=options.get("model"),
            status=SessionStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
        )
        self.sessions[session.session_id] = session
        return session

    async def cancel_session(self, session_id):
        self.cancelled.append(session_id)
        return session_id in self.sessions

    async def list_sessions(self, status=None):
        return [s for s in self.sessions.values() if status is None or s.status is status]

    async def get_session(self, session_id):
        return self.sessions.get(session_id)

    async def resolve_interaction(self, *args, **kwargs):
        return False

    def get_user_input_question_ids(self, interaction_id):
        return ()

    async def expire_due_interactions(self, now=None):
        self.expiry_calls += 1
        return []


class FakeConnection:
    def __init__(self):
        self.events = asyncio.Queue()
        self.terminal_error = None

    async def start(self):
        return None

    async def close(self):
        return None


class FakeLark:
    def send_rendered(self, message):
        return "message-1"

    def close(self):
        return None


def _runtime(tmp_path):
    store = FakeStore()
    codex = FakeAdapter(AgentKind.CODEX)
    claude = FakeAdapter(AgentKind.CLAUDE)
    registry = AgentRegistry()
    registry.register(codex)
    registry.register(claude)
    settings = SimpleNamespace(
        outbox_poll_seconds=0.01,
        interaction_expiry_poll_seconds=0.01,
        notification_delay_seconds=0.0,
        daemon_token_path=tmp_path / "daemon.token",
        message_format="card",
    )
    runtime = DaemonRuntime(
        settings,
        store,
        codex,
        FakeLark(),
        FakeConnection(),
        SimpleNamespace(route=lambda event: None),
        agent_registry=registry,
        now=lambda: NOW,
    )
    return runtime, store, codex, claude


def test_agent_session_create_bounds_and_repr_excludes_prompt():
    request = AgentSessionCreate(name="job", cwd=".", prompt="secret")
    assert "secret" not in repr(request)
    assert request.resume_id is None
    try:
        AgentSessionCreate(name="", cwd=".", prompt="secret")
    except ValidationError:
        pass
    else:
        raise AssertionError("empty name must be rejected")


def test_generic_claude_crud_status_resume_and_prompt_exclusion(workspace_tmp_path):
    runtime, _, _, claude = _runtime(workspace_tmp_path)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_daemon_app(runtime, token="secret")) as client:
        response = client.post(
            "/api/v1/agents/claude/sessions",
            headers=headers,
            json={"name": "claude job", "cwd": ".", "prompt": "TOP SECRET", "resume_id": "resume-1"},
        )
        assert response.status_code == 201
        assert "TOP SECRET" not in response.text
        assert claude.created[0]["resume_id"] == "resume-1"
        assert client.get("/api/v1/agents/claude/sessions", headers=headers, params={"status": "running"}).status_code == 200
        assert client.get("/api/v1/agents/claude/sessions/claude-1", headers=headers).status_code == 200
        assert client.post("/api/v1/agents/claude/sessions/claude-1/cancel", headers=headers).status_code == 200


def test_generic_hook_derives_provider_and_drops_sensitive_fields(workspace_tmp_path):
    runtime, store, _, _ = _runtime(workspace_tmp_path)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_daemon_app(runtime, token="secret")) as client:
        response = client.post(
            "/api/v1/agents/claude/hooks",
            headers=headers,
            json={
                "agent": "codex",
                "hook_event_name": "Stop",
                "event_id": "evt-1",
                "prompt": "secret prompt",
                "transcript": "secret transcript",
                "cwd": "C:/private",
                "tool_input": {"password": "secret"},
            },
        )
        assert response.status_code in {202, 422}
        if response.status_code == 202:
            assert all("secret" not in str(item) for item in store.items)


def test_invalid_provider_is_rejected(workspace_tmp_path):
    runtime, *_ = _runtime(workspace_tmp_path)
    with TestClient(create_daemon_app(runtime, token="secret")) as client:
        assert client.get("/api/v1/agents/unknown/sessions", headers={"Authorization": "Bearer secret"}).status_code in {404, 422}
