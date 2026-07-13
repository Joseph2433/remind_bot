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


def test_codex_hook_daemon_forward_has_short_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "lark_bot.cli.httpx.post",
        lambda *args, **kwargs: seen.append(kwargs) or type("Response", (), {"raise_for_status": lambda self: None})(),
    )
    monkeypatch.setattr("lark_bot.cli.ensure_daemon_token", lambda path: "token")

    result = CliRunner().invoke(
        app,
        ["codex-hook"],
        input=json.dumps({"hook_event_name": "Stop", "event_id": "event-1"}),
    )

    assert result.exit_code == 0
    assert seen[0]["timeout"] <= 0.25


def test_bare_codex_launches_native_tui_and_forwards_arguments(monkeypatch):
    seen = []

    def run(self, options):
        seen.append(options.args)
        return 9

    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", run)
    result = CliRunner().invoke(app, ["codex", "--model", "gpt-test", "hello"])

    assert result.exit_code == 9
    assert seen == [["--model", "gpt-test", "hello"]]


def test_bare_codex_without_arguments_launches_native_tui(monkeypatch):
    seen = []
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", lambda self, options: seen.append(options.args) or 0)

    result = CliRunner().invoke(app, ["codex"])

    assert result.exit_code == 0
    assert seen == [[]]


def test_codex_resume_is_forwarded_to_native_tui(monkeypatch):
    seen = []
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", lambda self, options: seen.append(options.args) or 0)

    result = CliRunner().invoke(app, ["codex", "resume", "--last"])

    assert result.exit_code == 0
    assert seen == [["resume", "--last"]]


def test_unattended_commands_live_under_codex_job(monkeypatch):
    monkeypatch.setattr("lark_bot.cli._daemon_request", lambda *args, **kwargs: {"id": "s1", "status": "running", "name": "task"})

    result = CliRunner().invoke(app, ["codex", "job", "start", "do work"])

    assert result.exit_code == 0
    assert "s1" in result.stdout
