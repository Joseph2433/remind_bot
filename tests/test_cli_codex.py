import json

from lark_bot.cli import build_codex_notification_from_json
from lark_bot.models import TaskStatus


def test_build_codex_notification_from_json_accepts_file_payload_shape():
    payload = {
        "name": "codex approval",
        "status": "needs_input",
        "stdout_tail": ["Need user input"],
    }

    request = build_codex_notification_from_json(json.dumps(payload))

    assert request.task.name == "codex approval"
    assert request.task.source == "codex"
    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT


def test_build_codex_notification_from_json_rejects_non_object_payload():
    try:
        build_codex_notification_from_json("[1, 2, 3]")
    except ValueError as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("expected ValueError")
