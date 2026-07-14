from lark_bot.tasks.detector import detect_output
from lark_bot.models import TaskStatus


def test_detects_waiting_for_manual_approval_case_insensitively():
    result = detect_output(
        "Build paused.\nDo you want to allow this command?\nwaiting for input",
        exit_code=0,
    )

    assert result.status is TaskStatus.WAITING_FOR_INPUT
    assert "approval" in result.tags
    assert "waiting_for_input" in result.tags


def test_detects_failure_from_exit_code_without_intervention_text():
    result = detect_output("tests failed", exit_code=2)

    assert result.status is TaskStatus.FAILED
    assert result.tags == ["failed"]


def test_detects_success_without_intervention_text():
    result = detect_output("all good", exit_code=0)

    assert result.status is TaskStatus.SUCCEEDED
    assert result.tags == ["succeeded"]


def test_does_not_treat_permission_denied_as_waiting_for_input():
    result = detect_output("Error: permission denied", exit_code=1)

    assert result.status is TaskStatus.FAILED
    assert result.tags == ["failed"]


def test_detects_permission_required_prompt():
    result = detect_output("permission required to continue", exit_code=0)

    assert result.status is TaskStatus.WAITING_FOR_INPUT
    assert "permission" in result.tags
