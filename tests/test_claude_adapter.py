import json

import pytest
from typer.testing import CliRunner

from lark_bot.cli import app
from lark_bot.modules.task.task_model import TaskStatus


def test_claude_stop_event_preserves_session_identity() -> None:
    from lark_bot.modules.claude.claude_adapter import (
        ClaudeEvent,
        claude_event_to_notification,
    )

    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="claude-session-1",
            session_name="docs",
            event_name="Stop",
            status="completed",
            summary="finished",
        )
    )

    assert request.context is not None
    assert request.context.session_id == "claude-session-1"
    assert request.context.session_name == "docs"
    assert request.context.agent.value == "claude"
    assert request.detection.status is TaskStatus.SUCCEEDED


def test_claude_failed_stop_maps_to_failure() -> None:
    from lark_bot.modules.claude.claude_adapter import (
        ClaudeEvent,
        claude_event_to_notification,
    )

    request = claude_event_to_notification(
        ClaudeEvent(
            sessionId="claude-session-2",
            name="tests",
            hook_event_name="Stop",
            status="failed",
            exit_code=1,
        )
    )

    assert request.context is not None
    assert request.context.session_id == "claude-session-2"
    assert request.detection.status is TaskStatus.FAILED


def test_claude_permission_request_maps_to_waiting() -> None:
    from lark_bot.modules.claude.claude_adapter import (
        ClaudeEvent,
        claude_event_to_notification,
    )

    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="claude-session-3",
            session_name="review",
            event_name="PermissionRequest",
            summary="allow command",
        )
    )

    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
    assert "claude" in request.detection.tags


def test_claude_permission_request_keeps_explicit_waiting_tag() -> None:
    from lark_bot.modules.claude.claude_adapter import (
        ClaudeEvent,
        claude_event_to_notification,
    )

    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="claude-session-waiting",
            session_name="review",
            event_name="PermissionRequest",
            summary="Approval required before continuing",
        )
    )

    assert request.detection.tags == [
        "claude",
        "PermissionRequest",
        "waiting_for_input",
    ]
    assert request.detection.matched_phrases == ["Approval"]


def test_claude_adapter_rejects_unsupported_hook_event() -> None:
    from lark_bot.modules.claude.claude_adapter import (
        ClaudeEvent,
        claude_event_to_notification,
    )

    with pytest.raises(ValueError, match="Unsupported Claude event"):
        claude_event_to_notification(
            ClaudeEvent(
                session_id="claude-session-4",
                session_name="bad",
                event_name="UnknownEvent",
            )
        )


def test_build_claude_notification_from_json_accepts_hook_aliases() -> None:
    from lark_bot.modules.claude.claude_service import build_claude_notification_from_json

    request = build_claude_notification_from_json(
        json.dumps(
            {
                "sessionId": "claude-session-5",
                "name": "aliases",
                "hook_event_name": "Stop",
                "status": "completed",
                "stdout_tail": ["done"],
            }
        )
    )

    assert request.context is not None
    assert request.context.session_id == "claude-session-5"
    assert request.task.stdout_tail == ["done"]


def test_claude_event_cli_uses_shared_notification_sender(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr("lark_bot.cli._send_with_dedupe", lambda request, settings: sent.append(request))

    result = CliRunner().invoke(
        app,
        ["claude-event"],
        input=json.dumps(
            {
                "session_id": "claude-session-6",
                "session_name": "cli",
                "event_name": "Stop",
                "status": "completed",
            }
        ),
    )

    assert result.exit_code == 0
    assert sent[0].context.session_id == "claude-session-6"
