from lack_bot.adapters.codex import CodexEvent, codex_event_to_notification
from lack_bot.models import TaskStatus


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
