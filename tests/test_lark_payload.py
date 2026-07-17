import json

import httpx
import pytest

from lark_bot.models import DetectionResult, NotificationRequest, TaskResult, TaskStatus
from lark_bot.lark.client import LarkAPIError, LarkBotClient, build_text_message


def test_build_text_message_uses_receive_id_and_json_content():
    payload = build_text_message("oc_test", "hello")

    assert payload["receive_id"] == "oc_test"
    assert payload["msg_type"] == "text"
    assert json.loads(payload["content"]) == {"text": "hello"}


def test_notification_text_contains_summary_without_secret():
    task = TaskResult(
        name="codex task",
        command=["codex"],
        exit_code=1,
        duration_seconds=2.5,
        stdout_tail=["token=abc123", "Need user input"],
        stderr_tail=[],
    )
    detection = DetectionResult(
        status=TaskStatus.WAITING_FOR_INPUT,
        tags=["waiting_for_input"],
        matched_phrases=["Need user input"],
    )
    request = NotificationRequest(task=task, detection=detection)

    text = LarkBotClient.render_notification_text(request, tail_lines=5)

    assert "codex task" in text
    assert "waiting_for_input" in text
    assert "abc123" not in text
    assert "[REDACTED]" in text


def test_send_request_defaults_to_interactive_card():
    captured = {}

    def handler(request):
        if "tenant_access_token" in str(request.url):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_card"}})

    task = TaskResult(
        name="codex task",
        command=["codex"],
        exit_code=0,
        duration_seconds=1.0,
        stdout_tail=["ok"],
        stderr_tail=[],
    )
    request = NotificationRequest(
        task=task,
        detection=DetectionResult(status=TaskStatus.SUCCEEDED, tags=["succeeded"]),
    )
    client = LarkBotClient(
        "id",
        "secret",
        "chat",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        message_format="card",
    )

    assert client.send(request) == "om_card"
    assert captured["payload"]["msg_type"] == "interactive"
    card = json.loads(captured["payload"]["content"])
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "green"


@pytest.mark.parametrize("payload, expected_error", [
    ({"code": 0, "data": {"message_id": "om_1"}}, None),
    ({"code": 0, "data": {}}, LarkAPIError),
])
def test_send_text_returns_required_message_id(payload, expected_error):
    def handler(request):
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token", "expire": 7200})
        return httpx.Response(200, json=payload)

    client = LarkBotClient("id", "secret", "chat", client=httpx.Client(transport=httpx.MockTransport(handler)))
    if expected_error:
        with pytest.raises(expected_error):
            client.send_text("hello")
    else:
        assert client.send_text("hello") == "om_1"
