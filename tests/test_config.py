from pathlib import Path

from lack_bot.config import Settings, build_config_checks, public_settings_summary


def test_build_config_checks_reports_missing_required_lark_values():
    settings = Settings(
        lark_app_id="",
        lark_app_secret="",
        lark_receive_id="",
        sqlite_path=Path(".lack-bot/test.sqlite3"),
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
