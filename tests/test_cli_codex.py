import json

import pytest
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
    requests = []

    def daemon_request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        if method == "POST":
            return {"session_id": "s1", "endpoint": "ws://127.0.0.1:9000", "remote_auth_token": "token"}
        return None

    def run(self, options):
        seen.append(options)
        return 9

    monkeypatch.setattr("lark_bot.cli._daemon_request", daemon_request)
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", run)
    result = CliRunner().invoke(app, ["codex", "--model", "gpt-test", "hello"])

    assert result.exit_code == 9
    assert seen[0].args == ["--model", "gpt-test", "hello"]
    assert seen[0].remote_endpoint == "ws://127.0.0.1:9000"
    assert seen[0].remote_auth_token == "token"
    assert requests[0][0:2] == ("POST", "/interactive-sessions")
    assert requests[-1][0:2] == ("DELETE", "/interactive-sessions/s1")


def test_bare_codex_without_arguments_launches_native_tui(monkeypatch):
    seen = []
    monkeypatch.setattr("lark_bot.cli._daemon_request", lambda method, path, **kwargs: {"session_id": "s1", "endpoint": "ws://127.0.0.1:1", "remote_auth_token": "t"} if method == "POST" else None)
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", lambda self, options: seen.append(options.args) or 0)

    result = CliRunner().invoke(app, ["codex"])

    assert result.exit_code == 0
    assert seen == [[]]


def test_codex_resume_is_forwarded_to_native_tui(monkeypatch):
    seen = []
    monkeypatch.setattr("lark_bot.cli._daemon_request", lambda method, path, **kwargs: {"session_id": "s1", "endpoint": "ws://127.0.0.1:1", "remote_auth_token": "t"} if method == "POST" else None)
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", lambda self, options: seen.append(options.args) or 0)

    result = CliRunner().invoke(app, ["codex", "resume", "--last"])

    assert result.exit_code == 0
    assert seen == [["resume", "--last"]]


def test_codex_resume_picker_requires_explicit_degradation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "lark_bot.cli._daemon_request",
        lambda *args, **kwargs: calls.append(("daemon", args, kwargs)),
    )
    monkeypatch.setattr(
        "lark_bot.cli.CodexTuiLauncher.run",
        lambda self, options: calls.append(("launcher", options)) or 0,
    )

    result = CliRunner().invoke(app, ["codex", "resume"])

    assert result.exit_code == 2
    assert "resume --last" in result.output
    assert "--no-lark" in result.output
    assert calls == []


@pytest.mark.parametrize(
    "picker_args",
    [
        ["resume", "--all"],
        ["resume", "--include-non-interactive"],
        ["resume", "--model", "gpt-test"],
    ],
)
def test_codex_resume_picker_options_are_rejected_before_daemon(monkeypatch, picker_args):
    calls = []
    monkeypatch.setattr(
        "lark_bot.cli._daemon_request",
        lambda *args, **kwargs: calls.append(("daemon", args, kwargs)),
    )
    monkeypatch.setattr(
        "lark_bot.cli.CodexTuiLauncher.run",
        lambda self, options: calls.append(("launcher", options)) or 0,
    )

    result = CliRunner().invoke(app, ["codex", *picker_args])

    assert result.exit_code == 2
    assert "session picker" in result.output.lower()
    assert calls == []


def test_codex_resume_explicit_session_is_forwarded_through_remote_daemon(monkeypatch):
    seen = []
    requests = []

    def daemon_request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        if method == "POST":
            return {"session_id": "s1", "endpoint": "ws://127.0.0.1:9000", "remote_auth_token": "token"}
        return None

    monkeypatch.setattr("lark_bot.cli._daemon_request", daemon_request)
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", lambda self, options: seen.append(options) or 0)

    result = CliRunner().invoke(app, ["codex", "resume", "session-name"])

    assert result.exit_code == 0
    assert seen[0].args == ["resume", "session-name"]
    assert seen[0].remote_endpoint == "ws://127.0.0.1:9000"
    assert requests[0][0:2] == ("POST", "/interactive-sessions")
    assert requests[-1][0:2] == ("DELETE", "/interactive-sessions/s1")


def test_codex_no_lark_launches_directly_without_daemon(monkeypatch):
    seen = []
    monkeypatch.setattr("lark_bot.cli._daemon_request", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("daemon must not be called")))
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", lambda self, options: seen.append(options) or 0)

    result = CliRunner().invoke(app, ["codex", "--no-lark", "hello"])

    assert result.exit_code == 0
    assert seen[0].args == ["hello"]
    assert seen[0].remote_endpoint is None
    assert seen[0].callback_command == []


def test_codex_no_lark_resume_launches_directly_without_daemon(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "lark_bot.cli._daemon_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("daemon must not be called")),
    )
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", lambda self, options: seen.append(options) or 0)

    result = CliRunner().invoke(app, ["codex", "--no-lark", "resume"])

    assert result.exit_code == 0
    assert seen[0].args == ["resume"]
    assert seen[0].remote_endpoint is None
    assert seen[0].callback_command == []


def test_codex_cleanup_failure_does_not_mask_tui_exit_code(monkeypatch):
    def daemon_request(method, path, **kwargs):
        if method == "POST":
            return {"session_id": "s1", "endpoint": "ws://127.0.0.1:1", "remote_auth_token": "t"}
        raise RuntimeError("cleanup detail")

    monkeypatch.setattr("lark_bot.cli._daemon_request", daemon_request)
    monkeypatch.setattr("lark_bot.cli.CodexTuiLauncher.run", lambda self, options: 7)

    result = CliRunner().invoke(app, ["codex"])

    assert result.exit_code == 7


def test_unattended_commands_live_under_codex_job(monkeypatch):
    monkeypatch.setattr("lark_bot.cli._daemon_request", lambda *args, **kwargs: {"id": "s1", "status": "running", "name": "task"})

    result = CliRunner().invoke(app, ["codex", "job", "start", "do work"])

    assert result.exit_code == 0
    assert "s1" in result.stdout
