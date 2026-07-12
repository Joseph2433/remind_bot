import json

from lark_bot.models import DetectionResult, NotificationRequest, TaskResult, TaskStatus
from lark_bot.notifier.lark import LarkBotClient, build_text_message


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
