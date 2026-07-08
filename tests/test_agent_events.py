from fastapi.testclient import TestClient

from lack_bot.models import NotificationRequest, TaskStatus
from lack_bot.server.app import create_app


class RecordingNotifier:
    def __init__(self) -> None:
        self.requests: list[NotificationRequest] = []

    def send(self, request: NotificationRequest) -> None:
        self.requests.append(request)


class AllowingStore:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str]] = []

    def should_send(self, dedupe_key: str, cooldown_seconds: int, now=None) -> bool:
        return True

    def record_sent(self, dedupe_key: str, status: str, now=None) -> None:
        self.recorded.append((dedupe_key, status))


class SuppressingStore(AllowingStore):
    def should_send(self, dedupe_key: str, cooldown_seconds: int, now=None) -> bool:
        return False


def test_agent_event_sends_structured_notification():
    app = create_app()
    notifier = RecordingNotifier()
    store = AllowingStore()
    app.state.notifier = notifier
    app.state.notification_store = store
    app.state.cooldown_seconds = 300
    client = TestClient(app)

    response = client.post(
        "/agent/events",
        json={
            "name": "claude code task",
            "source": "claude_hook",
            "status": "failed",
            "exit_code": 2,
            "duration_seconds": 12.5,
            "stdout_tail": ["Need user input", "token=abc123"],
            "stderr_tail": ["boom"],
            "tags": ["hook"],
        },
    )

    assert response.status_code == 200
    assert response.json()["sent"] is True
    assert len(notifier.requests) == 1
    request = notifier.requests[0]
    assert request.task.name == "claude code task"
    assert request.task.source == "claude_hook"
    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
    assert "waiting_for_input" in request.detection.tags
    assert store.recorded[0][1] == "waiting_for_input"


def test_agent_event_respects_dedupe_store():
    app = create_app()
    notifier = RecordingNotifier()
    app.state.notifier = notifier
    app.state.notification_store = SuppressingStore()
    app.state.cooldown_seconds = 300
    client = TestClient(app)

    response = client.post(
        "/agent/events",
        json={
            "name": "codex task",
            "status": "succeeded",
            "exit_code": 0,
            "duration_seconds": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["sent"] is False
    assert notifier.requests == []
