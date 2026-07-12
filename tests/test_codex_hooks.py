import json

from lark_bot.hooks import check_hooks, install_hooks, uninstall_hooks


def test_hooks_merge_idempotently_and_preserve_unrelated(workspace_tmp_path):
    path = workspace_tmp_path / ".codex" / "hooks.json"; path.parent.mkdir()
    path.write_text(json.dumps({"Other": [{"hooks": [{"type": "command", "command": "keep"}]}], "custom": 1}), encoding="utf-8")
    install_hooks(workspace_tmp_path); first = path.read_text(encoding="utf-8"); install_hooks(workspace_tmp_path)
    assert path.read_text(encoding="utf-8") == first and check_hooks(workspace_tmp_path).status == "installed"
    uninstall_hooks(workspace_tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"Other": [{"hooks": [{"type": "command", "command": "keep"}]}], "custom": 1}


def test_hooks_check_malformed_json(workspace_tmp_path):
    path = workspace_tmp_path / ".codex" / "hooks.json"; path.parent.mkdir(); path.write_text("{", encoding="utf-8")
    assert check_hooks(workspace_tmp_path).status == "malformed"


def test_uninstall_preserves_unrelated_nested_commands(workspace_tmp_path):
    path = workspace_tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": "lark-bot codex-hook", "async": True},
                            {"type": "command", "command": "keep"},
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    uninstall_hooks(workspace_tmp_path)
    remaining = json.loads(path.read_text(encoding="utf-8"))
    assert remaining["Stop"][0]["hooks"] == [{"type": "command", "command": "keep"}]


def test_install_repairs_modified_owned_entry_and_preserves_unrelated(workspace_tmp_path):
    path = workspace_tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    unrelated = {"matcher": "custom", "hooks": [{"type": "command", "command": "keep"}]}
    path.write_text(
        json.dumps(
            {
                "SessionStart": [
                    {"matcher": "wrong", "hooks": [{"type": "command", "command": "lark-bot codex-hook", "async": False}]},
                    unrelated,
                ]
            }
        ),
        encoding="utf-8",
    )
    assert check_hooks(workspace_tmp_path).status == "modified"
    install_hooks(workspace_tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert check_hooks(workspace_tmp_path).status == "installed"
    assert unrelated in data["SessionStart"]
    assert sum(
        hook.get("command") == "lark-bot codex-hook"
        for entry in data["SessionStart"]
        for hook in entry.get("hooks", [])
    ) == 1
