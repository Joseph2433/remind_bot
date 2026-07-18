import pytest
from pydantic import ValidationError

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


@pytest.mark.parametrize(
    "notification_type",
    ["permission_prompt", "idle_prompt", "agent_needs_input", "elicitation_dialog"],
)
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


@pytest.mark.parametrize(
    "notification_type",
    ["auth_success", "elicitation_complete", "elicitation_response", "agent_completed"],
)
def test_observational_notifications_are_completed(notification_type: str) -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="Notification",
            notification_type=notification_type,
        )
    )
    assert request.detection.status is TaskStatus.COMPLETED
    assert notification_type in request.detection.tags or "turn_completed" in request.detection.tags


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
        prompt="prompt_sentinel",
        transcript_path="transcript_path_sentinel",
        cwd="cwd_sentinel",
        tool_name="tool_name_sentinel",
        tool_input={"secret": "tool_input_sentinel"},
        permission_suggestions=["permission_suggestions_sentinel"],
        last_assistant_message="last_assistant_message_sentinel",
        error_details={"password": "error_details_sentinel"},
    )
    request = claude_event_to_notification(event)
    serialized = request.model_dump_json()
    for secret in (
        "prompt_sentinel",
        "transcript_path_sentinel",
        "cwd_sentinel",
        "tool_name_sentinel",
        "tool_input_sentinel",
        "permission_suggestions_sentinel",
        "last_assistant_message_sentinel",
        "error_details_sentinel",
    ):
        assert secret not in serialized


def test_repeated_raw_permission_request_replay_is_idempotent() -> None:
    first = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="PermissionRequest",
            prompt_id="prompt-1",
            tool_name="bash",
            tool_input={"command": "echo one", "z": 2},
        )
    )
    second = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="PermissionRequest",
            prompt_id="prompt-1",
            tool_name="bash",
            tool_input={"z": 2, "command": "echo one"},
        )
    )

    assert first.event_id == second.event_id
    assert first.dedupe_key == second.dedupe_key
    serialized = first.model_dump_json()
    assert "bash" not in serialized
    assert "echo one" not in serialized


def test_repeated_raw_waiting_notification_replay_is_idempotent() -> None:
    event = ClaudeEvent(
        session_id="session",
        hook_event_name="Notification",
        prompt_id="prompt-1",
        notification_type="permission_prompt",
    )

    first = claude_event_to_notification(event)
    second = claude_event_to_notification(event)

    assert first.event_id == second.event_id


def test_explicit_safe_event_id_keeps_action_request_idempotent() -> None:
    first = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="PermissionRequest",
            prompt_id="prompt-1",
            event_id="safe-event-1",
            tool_input={"command": "private one"},
        )
    )
    second = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="PermissionRequest",
            prompt_id="prompt-1",
            event_id="safe-event-1",
            tool_input={"command": "private two"},
        )
    )

    assert first.event_id == "safe-event-1"
    assert second.event_id == first.event_id
    assert second.dedupe_key == first.dedupe_key


def test_distinct_explicit_safe_event_ids_distinguish_action_requests() -> None:
    first = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="PermissionRequest",
            prompt_id="prompt-1",
            event_id="safe-event-1",
        )
    )
    second = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="PermissionRequest",
            prompt_id="prompt-1",
            event_id="safe-event-2",
        )
    )

    assert first.event_id != second.event_id
    assert first.dedupe_key != second.dedupe_key


def test_claude_event_strips_whitespace_before_validation() -> None:
    event = ClaudeEvent(
        session_id=" session ",
        hook_event_name=" Stop ",
        event_id=" safe-event ",
    )

    assert event.session_id == "session"
    assert event.hook_event_name == "Stop"
    assert event.event_id == "safe-event"


@pytest.mark.parametrize("field_name", ["session_id", "hook_event_name", "event_id"])
def test_claude_event_rejects_whitespace_only_identifiers(field_name: str) -> None:
    values = {"session_id": "session", "hook_event_name": "Stop", field_name: "   "}

    with pytest.raises(ValidationError):
        ClaudeEvent(**values)


def test_claude_event_id_accepts_200_characters_and_rejects_201() -> None:
    accepted = ClaudeEvent(
        session_id="session",
        hook_event_name="Stop",
        event_id="x" * 200,
    )
    assert accepted.event_id == "x" * 200

    with pytest.raises(ValidationError):
        ClaudeEvent(
            session_id="session",
            hook_event_name="Stop",
            event_id="x" * 201,
        )


def test_deterministic_event_identity_avoids_delimiter_collisions() -> None:
    first = claude_event_to_notification(
        ClaudeEvent(session_id="a|b", hook_event_name="Stop")
    )
    second = claude_event_to_notification(
        ClaudeEvent(session_id="a", prompt_id="b|-", hook_event_name="Stop")
    )

    assert first.event_id != second.event_id


def test_deterministic_event_identity_normalizes_safe_discriminator() -> None:
    first = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name=" Notification ",
            notification_type=" AUTH_SUCCESS ",
        )
    )
    second = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="notification",
            notification_type="auth_success",
        )
    )

    assert first.event_id == second.event_id


def test_session_end_uses_redacted_reason_summary() -> None:
    request = claude_event_to_notification(
        ClaudeEvent(
            session_id="session",
            hook_event_name="SessionEnd",
            reason="logout token=private-value",
        )
    )

    assert request.task.stdout_tail == ["logout token=[REDACTED]"]


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


@pytest.mark.parametrize("notification_type", ["unknown", "task_completed", "info", "warning", "error"])
def test_unknown_notification_type_fails_closed(notification_type: str) -> None:
    with pytest.raises(ValueError, match="Unsupported Claude notification type"):
        claude_event_to_notification(
            ClaudeEvent(
                session_id="session",
                hook_event_name="Notification",
                notification_type=notification_type,
            )
        )
