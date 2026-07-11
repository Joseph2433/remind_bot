import pytest

from lark_bot.adapters.codex import CodexEvent, codex_event_to_notification
from lark_bot.models import TaskStatus


def test_codex_adapter_maps_success_event_to_notification():
    event = CodexEvent(
        task_name="implement feature",
        status="completed",
        command=["codex"],
        duration_seconds=4.2,
        output_tail=["done"],
    )

    request = codex_event_to_notification(event)

    assert request.task.name == "implement feature"
    assert request.task.source == "codex"
    assert request.task.command == ["codex"]
    assert request.task.exit_code == 0
    assert request.task.stdout_tail == ["done"]
    assert request.detection.status is TaskStatus.SUCCEEDED
    assert "codex" in request.detection.tags


def test_codex_adapter_detects_waiting_for_input_from_status_and_output():
    event = CodexEvent(
        name="approval flow",
        status="approval_required",
        exit_code=0,
        output_tail=["Do you want to allow this command?"],
    )

    request = codex_event_to_notification(event)

    assert request.task.name == "approval flow"
    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
    assert "approval" in request.detection.tags
    assert "codex" in request.detection.tags


def test_codex_adapter_maps_failure_alias_to_failed_exit_code():
    event = CodexEvent(task_name="tests", status="error")

    request = codex_event_to_notification(event)

    assert request.task.exit_code == 1
    assert request.detection.status is TaskStatus.FAILED


def test_codex_adapter_waiting_defaults_to_exit_zero_and_waiting_tags():
    event = CodexEvent(task_name="approval", status="approval_required")

    request = codex_event_to_notification(event)

    assert request.task.exit_code == 0
    assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
    assert request.detection.tags == ["codex", "waiting_for_input"]


def test_codex_adapter_keeps_failed_status_for_permission_denied_errors():
    event = CodexEvent(
        task_name="write file",
        status="error",
        exit_code=1,
        stderr_tail=["Error: permission denied"],
    )

    request = codex_event_to_notification(event)

    assert request.detection.status is TaskStatus.FAILED
    assert "permission" not in request.detection.tags


def test_codex_adapter_rejects_unknown_and_progress_statuses():
    with pytest.raises(ValueError, match="Unsupported Codex status"):
        codex_event_to_notification(CodexEvent(task_name="mid", status="running"))


def test_codex_adapter_prefers_failed_when_success_status_has_nonzero_exit():
    event = CodexEvent(
        task_name="tests",
        status="succeeded",
        exit_code=1,
        output_tail=["ok"],
    )

    request = codex_event_to_notification(event)

    assert request.detection.status is TaskStatus.FAILED
    assert request.task.exit_code == 1
