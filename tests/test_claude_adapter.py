import json

import pytest
from typer.testing import CliRunner

from lark_bot.cli import app
from lark_bot.modules.claude.claude_adapter import ClaudeEvent, claude_event_to_notification
from lark_bot.modules.claude.claude_service import build_claude_notification_from_json
from lark_bot.modules.task.task_model import TaskStatus


def test_stop_hook_is_completed_with_turn_identity() -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="claude-session-1",
            hook_event_name="Stop",
            stop_hook_active=True,
        )
    )

    assert request.context is not None
    assert request.context.session_id == "claude-session-1"
    assert request.detection.status is TaskStatus.COMPLETED
    assert request.task.exit_code == 0
    assert "turn_completed" in request.detection.tags
    assert request.event_id


def test_stop_failure_maps_failed_without_synthetic_status() -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="claude-session-2",
            hook_event_name="StopFailure",
            error="model stopped unexpectedly",
        )
    )

    assert request.detection.status is TaskStatus.FAILED
    assert request.task.exit_code != 0
    assert "turn_failed" in request.detection.tags


def test_permission_request_maps_waiting() -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="claude-session-3",
            hook_event_name="PermissionRequest",
            message="allow command",
        )
    )

    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
    assert "waiting_for_input" in request.detection.tags


def test_unknown_hook_event_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported Claude event"):
        claude_event_to_notification(
            ClaudeEvent(session_id="claude-session-4", hook_event_name="UnknownEvent")
        )


def test_hook_aliases_are_accepted_but_synthetic_fields_are_ignored() -> None:
    request = build_claude_notification_from_json(
        json.dumps(
            {
                "sessionId": "claude-session-5",
                "event_name": "Stop",
                "status": "failed",
                "output_tail": ["should not be used"],
            }
        )
    )

    assert request.context is not None
    assert request.context.session_id == "claude-session-5"
    assert request.detection.status is TaskStatus.COMPLETED
    assert request.task.stdout_tail == []


def test_claude_event_cli_uses_shared_notification_sender(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr("lark_bot.cli._send_with_dedupe", lambda request, settings: sent.append(request))

    result = CliRunner().invoke(
        app,
        ["claude-event"],
        input=json.dumps(
            {
                "session_id": "claude-session-6",
                "hook_event_name": "Stop",
            }
        ),
    )

    assert result.exit_code == 0
    assert sent[0].context.session_id == "claude-session-6"
