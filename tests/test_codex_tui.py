from __future__ import annotations

import sys
import json

import pytest

from lark_bot.codex.tui import CodexTuiLauncher, CodexTuiOptions


def test_launcher_resolves_codex_and_inherits_console_streams(monkeypatch):
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr("lark_bot.codex.tui.shutil.which", lambda value: "C:/tools/codex.cmd")
    monkeypatch.setattr("lark_bot.codex.tui._read_existing_notify", lambda value: None)

    class Result:
        returncode = 7

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    launcher = CodexTuiLauncher(process_runner=run)
    exit_code = launcher.run(
        CodexTuiOptions(
            args=["resume", "--last"],
            callback_command=[sys.executable, "-m", "lark_bot", "codex-hook"],
        )
    )

    assert exit_code == 7
    args, kwargs = calls[0]
    assert args[:2] == ["C:/tools/codex.cmd", "-c"]
    assert args[3:] == ["resume", "--last"]
    assert kwargs == {}
    assert "notify=" in args[2]
    assert "codex-hook" in args[2]


def test_launcher_chains_existing_global_notify_without_editing_config(monkeypatch, workspace_tmp_path):
    config = workspace_tmp_path / "config.toml"
    config.write_text('model = "keep"\nnotify = ["C:\\\\tools\\\\existing.exe", "turn-ended"]\n', encoding="utf-8")
    monkeypatch.setattr("lark_bot.codex.tui.shutil.which", lambda value: "C:/tools/codex.cmd")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"returncode": 0})()

    CodexTuiLauncher(process_runner=run).run(CodexTuiOptions(config_path=config))

    assert config.read_text(encoding="utf-8").startswith('model = "keep"')
    chain = calls[0][1]["env"]["LARK_BOT_CODEX_NOTIFY_CHAIN"]
    assert json.loads(chain) == [r"C:\tools\existing.exe", "turn-ended"]


def test_launcher_forwards_prompt_model_and_sandbox_verbatim(monkeypatch):
    monkeypatch.setattr("lark_bot.codex.tui.shutil.which", lambda value: "/usr/bin/codex")
    monkeypatch.setattr("lark_bot.codex.tui._read_existing_notify", lambda value: None)
    seen: list[list[str]] = []

    def run(args, **kwargs):
        seen.append(args)
        return type("Result", (), {"returncode": 0})()

    launcher = CodexTuiLauncher(process_runner=run)
    launcher.run(CodexTuiOptions(args=["--model", "gpt-test", "--sandbox", "read-only", "hello world"]))

    assert seen[0][-5:] == ["--model", "gpt-test", "--sandbox", "read-only", "hello world"]


def test_launcher_reports_missing_codex(monkeypatch):
    monkeypatch.setattr("lark_bot.codex.tui.shutil.which", lambda value: None)

    with pytest.raises(FileNotFoundError, match="Codex executable"):
        CodexTuiLauncher().run(CodexTuiOptions())


def test_remote_launcher_uses_gateway_token_env_and_skips_notify(monkeypatch):
    monkeypatch.setattr("lark_bot.codex.tui.shutil.which", lambda value: "C:/tools/codex.exe")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"returncode": 3})()

    exit_code = CodexTuiLauncher(process_runner=run).run(
        CodexTuiOptions(
            args=["resume", "--last"],
            remote_endpoint="ws://127.0.0.1:4321",
            remote_auth_token="secret-token",
        )
    )

    assert exit_code == 3
    command, kwargs = calls[0]
    assert command == [
        "C:/tools/codex.exe",
        "--remote",
        "ws://127.0.0.1:4321",
        "--remote-auth-token-env",
        "LARK_BOT_CODEX_REMOTE_TOKEN",
        "resume",
        "--last",
    ]
    assert kwargs["env"]["LARK_BOT_CODEX_REMOTE_TOKEN"] == "secret-token"
    assert "-c" not in command


def test_remote_launcher_requires_endpoint_and_token_together(monkeypatch):
    monkeypatch.setattr("lark_bot.codex.tui.shutil.which", lambda value: "codex")

    with pytest.raises(ValueError, match="endpoint and token"):
        CodexTuiLauncher().run(CodexTuiOptions(remote_endpoint="ws://127.0.0.1:1"))
