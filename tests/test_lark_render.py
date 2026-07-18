import json

from lark_bot.codex.models import InteractionKind
from lark_bot.lark.messages import build_api_payload, build_interactive_message, interactive_card
from lark_bot.lark.render import render_outbox_notification, render_task_notification
from lark_bot.models import DetectionResult, NotificationRequest, TaskResult, TaskStatus


def _request(**kwargs) -> NotificationRequest:
    task = TaskResult(
        name=kwargs.get("name", "codex task"),
        command=kwargs.get("command", ["codex"]),
        exit_code=kwargs.get("exit_code", 0),
        duration_seconds=kwargs.get("duration_seconds", 1.5),
        stdout_tail=kwargs.get("stdout_tail", ["ok"]),
        stderr_tail=kwargs.get("stderr_tail", []),
        source=kwargs.get("source", "codex"),
    )
    detection = DetectionResult(
        status=kwargs.get("status", TaskStatus.SUCCEEDED),
        tags=kwargs.get("tags", ["succeeded"]),
    )
    return NotificationRequest(task=task, detection=detection)


def test_interactive_card_schema_shape():
    rendered = interactive_card(title="Hello", markdown="**body**", template="green")
    payload = build_interactive_message("oc_test", rendered.content)

    assert payload["msg_type"] == "interactive"
    card = json.loads(payload["content"])
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "Hello"
    assert card["body"]["elements"][0]["tag"] == "markdown"
    assert card["body"]["elements"][0]["content"] == "**body**"


def test_task_card_redacts_secrets_and_maps_status_color():
    request = _request(
        status=TaskStatus.WAITING_FOR_INPUT,
        tags=["waiting_for_input"],
        stdout_tail=["token=abc123", "Need user input"],
        exit_code=1,
    )
    rendered = render_task_notification(request, message_format="card", tail_lines=5)

    assert rendered.msg_type == "interactive"
    assert rendered.content["header"]["template"] == "orange"
    markdown = rendered.content["body"]["elements"][0]["content"]
    assert "abc123" not in markdown
    assert "[REDACTED]" in markdown
    assert "Need user input" in markdown


def test_task_text_format_preserves_plain_layout():
    request = _request(stdout_tail=["token=abc123"])
    rendered = render_task_notification(request, message_format="text", tail_lines=5)

    assert rendered.msg_type == "text"
    text = rendered.content["text"]
    assert "Lark Bot: succeeded" in text
    assert "abc123" not in text
    assert "[REDACTED]" in text


def test_outbox_approval_card_includes_instructions():
    item = type(
        "Item",
        (),
        {
            "notification_type": "orchestrator:interaction_requested",
            "payload_summary": "token=secret-value run ls",
            "interaction_id": "i1",
        },
    )()
    interaction = type("Interaction", (), {"kind": InteractionKind.EXEC_APPROVAL})()

    rendered = render_outbox_notification(item, message_format="card", interaction=interaction)

    assert rendered.msg_type == "interactive"
    assert rendered.content["header"]["title"]["content"] == "Codex 请求审批"
    assert rendered.content["header"]["template"] == "orange"
    body = rendered.content["body"]["elements"][0]["content"]
    assert "secret-value" not in body
    assert "[REDACTED]" in body
    assert "yes 或 y" in body
    assert "no 或 n" in body


def test_build_api_payload_stringifies_content():
    rendered = interactive_card(title="T", markdown="M", template="blue")
    payload = build_api_payload("oc_1", rendered)
    assert payload["receive_id"] == "oc_1"
    assert payload["msg_type"] == "interactive"
    assert isinstance(payload["content"], str)
    assert json.loads(payload["content"])["header"]["title"]["content"] == "T"
