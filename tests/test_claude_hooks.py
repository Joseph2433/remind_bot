import pytest

from lark_bot.modules.claude.claude_adapter import ClaudeEvent, claude_event_to_notification
from lark_bot.modules.task.task_model import TaskStatus


@pytest.mark.parametrize(
    ("event_name", "expected_status", "tag"),
    [
        ("SessionStart", TaskStatus.COMPLETED, "session_started"),
        ("SessionEnd", TaskStatus.COMPLETED, "session_ended"),
        ("UserPromptSubmit", TaskStatus.COMPLETED, "prompt_submitted"),
        ("Stop", TaskStatus.COMPLETED, "turn_completed"),
        ("StopFailure", TaskStatus.FAILED, "turn_failed"),
    ],
)
def test_supported_lifecycle_hooks(event_name: str, expected_status: TaskStatus, tag: str) -> None:
    request = claude_event_to_notification(
        ClaudeEvent(session_id="session", hook_event_name=event_name)
    )
    assert request.detection.status is expected_status
    assert tag in request.detection.tags


@pytest.mark.parametrize("notification_type", ["permission_prompt", "idle_prompt", "agent_needs_input"])
def test_action_required_notifications_wait_for_input(notification_type: str) -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="Notification",
            notification_type=notification_type,
        )
    )
    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
    assert "waiting_for_input" in request.detection.tags
    assert notification_type in request.detection.tags


def test_agent_completed_notification_is_completed() -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="Notification",
            notification_type="agent_completed",
        )
    )
    assert request.detection.status is TaskStatus.COMPLETED
    assert "turn_completed" in request.detection.tags


def test_user_prompt_submit_is_not_waiting() -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="UserPromptSubmit",
            prompt_id="prompt-1",
            message="hello",
        )
    )
    assert request.detection.status is TaskStatus.COMPLETED
    assert "waiting_for_input" not in request.detection.tags


def test_sensitive_hook_extras_are_not_stored_in_notification() -> None:
    event = ClaudeEvent(
        session_id="session",
        hook_event_name="Stop",
        prompt="do not expose",
        transcript_path="/private/transcript.jsonl",
        cwd="C:\\private",
        tool_input={"token": "secret"},
        permission_suggestions=["grant"],
        last_assistant_message="private transcript",
        error_details={"password": "secret"},
    )
    request = claude_event_to_notification(event)
    serialized = request.model_dump_json()
    for secret in ("do not expose", "private/transcript", "private transcript", "secret"):
        assert secret not in serialized


def test_repeated_permissions_with_distinct_prompt_ids_have_distinct_dedupe_keys() -> None:
    first = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="PermissionRequest",
            prompt_id="prompt-1",
        )
    )
    second = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="PermissionRequest",
            prompt_id="prompt-2",
        )
    )
    assert first.dedupe_key != second.dedupe_key


def test_unknown_notification_type_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported Claude notification type"):
        claude_event_to_notification(
            ClaudeEvent(
                session_id="session",
                hook_event_name="Notification",
                notification_type="unknown",
            )
        )
