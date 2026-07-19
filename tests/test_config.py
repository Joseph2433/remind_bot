from pathlib import Path

from lark_bot.config import Settings, build_config_checks, public_settings_summary


def test_build_config_checks_reports_missing_required_lark_values():
    settings = Settings(
        lark_app_id="",
        lark_app_secret="",
        lark_receive_id="",
        sqlite_path=Path(".lark-bot/test.sqlite3"),
    )

    checks = build_config_checks(settings)
    failed_names = {check.name for check in checks if not check.ok}

    assert "lark_app_id" in failed_names
    assert "lark_app_secret" in failed_names
    assert "lark_receive_id" in failed_names


def test_public_settings_summary_redacts_secret_values():
    settings = Settings(
        lark_app_id="cli_real",
        lark_app_secret="super-secret",
        lark_receive_id="oc_real",
    )

    summary = public_settings_summary(settings)

    assert summary["lark_app_secret"] == "[set]"
    assert "super-secret" not in str(summary)
    assert summary["lark_app_id"] == "cli_real"


def test_daemon_settings_have_safe_defaults():
    settings = Settings()
    assert settings.daemon_host == "127.0.0.1"
    assert settings.daemon_port == 8787
    assert settings.codex_path == "codex"
    assert settings.claude_path == "claude"
    assert settings.interaction_timeout_seconds == 1800
    assert settings.interaction_expiry_poll_seconds == 1.0
    assert settings.outbox_poll_seconds == 0.5
    assert settings.notification_delay_seconds == 0.0
    assert settings.message_format == "card"


def test_message_format_can_be_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("LARK_BOT_MESSAGE_FORMAT", "text")

    settings = Settings()

    assert settings.message_format == "text"
    assert public_settings_summary(settings)["message_format"] == "text"


def test_notification_delay_can_be_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("LARK_BOT_NOTIFICATION_DELAY_SECONDS", "2.5")

    settings = Settings()

    assert settings.notification_delay_seconds == 2.5
    assert public_settings_summary(settings)["notification_delay_seconds"] == 2.5


def test_config_checks_report_claude_runtime_without_leaking_paths_or_errors():
    secret_path = r"C:\Users\private-user\bin\claude.exe"

    def package_version(name: str) -> str:
        assert name == "claude-agent-sdk"
        raise RuntimeError("credential=secret package failure")

    settings = Settings(claude_path=secret_path)
    checks = build_config_checks(
        settings,
        executable_lookup=lambda command: secret_path,
        package_version=package_version,
    )
    by_name = {check.name: check for check in checks}

    assert by_name["claude_executable"].ok is True
    assert by_name["claude_agent_sdk"].ok is False
    rendered = str([check.model_dump() for check in checks])
    assert secret_path not in rendered
    assert "private-user" not in rendered
    assert "credential=secret" not in rendered


def test_config_checks_accept_installed_claude_sdk_and_hide_configured_path():
    settings = Settings(claude_path=r"D:\tools\claude.exe")
    checks = build_config_checks(
        settings,
        executable_lookup=lambda command: command,
        package_version=lambda name: "0.9.0",
    )

    by_name = {check.name: check for check in checks}
    assert by_name["claude_executable"].ok is True
    assert by_name["claude_agent_sdk"].ok is True
    summary = public_settings_summary(settings)
    assert summary["claude_path"] == "[configured]"
    assert "D:\\tools" not in str(summary)
