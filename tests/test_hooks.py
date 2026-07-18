from lark_bot.modules.codex.codex_hook import build_notify_override, check_hooks, install_hooks, uninstall_hooks


def test_notify_override_is_valid_toml_and_quotes_windows_paths():
    override = build_notify_override([r"C:\Program Files\Python\python.exe", "-m", "lark_bot", "codex-hook"])

    assert override.startswith("notify=")
    assert '"C:\\\\Program Files\\\\Python\\\\python.exe"' in override


def test_hook_installer_writes_a_fragment_without_touching_user_config(workspace_tmp_path):
    config = workspace_tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "keep-me"\n', encoding="utf-8")

    installed = install_hooks(workspace_tmp_path)

    assert installed.name == "lark-bot-notify.toml"
    assert config.read_text(encoding="utf-8") == 'model = "keep-me"\n'
    assert "notify" in installed.read_text(encoding="utf-8")
    assert check_hooks(workspace_tmp_path).status == "installed"
    uninstall_hooks(workspace_tmp_path)
    assert not installed.exists()


def test_check_reports_modified_fragment(workspace_tmp_path):
    path = workspace_tmp_path / ".codex" / "lark-bot-notify.toml"
    path.parent.mkdir()
    path.write_text('notify = ["other"]\n', encoding="utf-8")

    assert check_hooks(workspace_tmp_path).status == "modified"
