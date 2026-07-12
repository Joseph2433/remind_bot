import json

from typer.testing import CliRunner

from lark_bot.cli import app, build_codex_notification_from_json
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


def test_build_codex_notification_from_json_rejects_progress_status():
    payload = {"name": "mid", "status": "in_progress"}
    try:
        build_codex_notification_from_json(json.dumps(payload))
    except ValueError as exc:
        assert "Unsupported Codex status" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_codex_hooks_install_and_check(workspace_tmp_path):
    runner = CliRunner()
    installed = runner.invoke(app, ["codex", "hooks", "install", "--project", str(workspace_tmp_path)])
    assert installed.exit_code == 0
    checked = runner.invoke(app, ["codex", "hooks", "check", "--project", str(workspace_tmp_path)])
    assert checked.exit_code == 0 and "installed" in checked.stdout


def test_codex_hook_ignores_invalid_stdin():
    result = CliRunner().invoke(app, ["codex-hook"], input="not-json")
    assert result.exit_code == 0
