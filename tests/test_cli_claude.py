from __future__ import annotations

import json

from typer.testing import CliRunner

from lark_bot.commands import app as commands


def test_claude_native_forwards_args_without_daemon(monkeypatch):
    seen = []
    monkeypatch.setattr(commands, "get_settings", lambda: type("S", (), {"claude_path": "claude"})())
    monkeypatch.setattr(commands.ClaudeTuiLauncher, "run", lambda self, options: seen.append(options) or 7)

    result = CliRunner().invoke(commands.app, ["claude", "--resume", "session-1", "--model", "opus"])

    assert result.exit_code == 7
    assert seen[0].args == ["--resume", "session-1", "--model", "opus"]
    assert seen[0].env is None


def test_claude_no_lark_sets_disabled_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(commands, "get_settings", lambda: type("S", (), {"claude_path": "claude"})())
    monkeypatch.setattr(commands.ClaudeTuiLauncher, "run", lambda self, options: seen.append(options) or 0)

    result = CliRunner().invoke(commands.app, ["claude", "--no-lark", "--continue"])

    assert result.exit_code == 0
    assert seen[0].args == ["--continue"]
    assert seen[0].env["LARK_BOT_CLAUDE_HOOK_DISABLED"] == "1"


def test_claude_jobs_use_generic_agent_routes(monkeypatch):
    requests = []

    def request(agent, method, path, **kwargs):
        requests.append((agent, method, path, kwargs.get("json_body")))
        return {"id": "s1", "status": "running", "name": "job"}

    monkeypatch.setattr(commands, "_agent_daemon_request", request)
    runner = CliRunner()
    started = runner.invoke(
        commands.app,
        ["claude", "job", "start", "--name", "job", "--model", "opus", "--permission", "accept", "--resume", "r1", "-"],
        input="private prompt\n",
    )
    listed = runner.invoke(commands.app, ["claude", "job", "list", "--status", "running"])
    shown = runner.invoke(commands.app, ["claude", "job", "show", "s1"])
    cancelled = runner.invoke(commands.app, ["claude", "job", "cancel", "s1"])

    assert all(result.exit_code == 0 for result in (started, listed, shown, cancelled))
    assert requests[0] == (
        "claude",
        "POST",
        "/sessions",
        {
            "name": "job",
            "cwd": str(__import__("pathlib").Path(".").resolve()),
            "prompt": "private prompt\n",
            "model": "opus",
            "sandbox": "workspace-write",
            "permission_mode": "accept",
            "resume_id": "r1",
        },
    )
    assert requests[1][2] == "/sessions?status=running"
    assert requests[2][2] == "/sessions/s1"
    assert requests[3][2] == "/sessions/s1/cancel"


def test_claude_hooks_delegate_to_installer(monkeypatch, workspace_tmp_path):
    calls = []
    monkeypatch.setattr(commands, "install_claude_hooks", lambda project: calls.append(("install", project)) or type("R", (), {"status": "installed"})())
    monkeypatch.setattr(commands, "check_claude_hooks", lambda project: calls.append(("check", project)) or type("R", (), {"status": "installed"})())
    monkeypatch.setattr(commands, "uninstall_claude_hooks", lambda project: calls.append(("uninstall", project)) or type("R", (), {"status": "missing"})())
    runner = CliRunner()
    for action in ("install", "check", "uninstall"):
        result = runner.invoke(commands.app, ["claude", "hooks", action, "--project", str(workspace_tmp_path)])
        assert result.exit_code == 0
    assert [item[0] for item in calls] == ["install", "check", "uninstall"]
