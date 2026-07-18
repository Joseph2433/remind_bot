from __future__ import annotations

import json
from pathlib import Path

import pytest

from lark_bot.modules.claude.claude_hook_installer import (
    OWNED_HANDLER,
    check_hooks,
    install_hooks,
    uninstall_hooks,
)


def test_install_preserves_unrelated_settings_and_is_idempotent(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]},
            }
        ),
        encoding="utf-8",
    )

    install_hooks(tmp_path)
    first = settings.read_text(encoding="utf-8")
    install_hooks(tmp_path)

    value = json.loads(first)
    assert value["permissions"] == {"allow": ["Read"]}
    assert value["hooks"]["Stop"][0]["hooks"][0]["command"] == "other"
    assert settings.read_text(encoding="utf-8") == first
    assert check_hooks(tmp_path).status == "installed"


def test_uninstall_removes_only_owned_handlers(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    install_hooks(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "other"}]})
    settings.write_text(json.dumps(data), encoding="utf-8")

    uninstall_hooks(tmp_path)
    value = json.loads(settings.read_text(encoding="utf-8"))
    assert value["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "other"}]}]
    assert check_hooks(tmp_path).status == "missing"


def test_uninstall_prunes_empty_owned_matcher_group(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [OWNED_HANDLER]}]}}),
        encoding="utf-8",
    )

    uninstall_hooks(tmp_path)

    assert json.loads(settings.read_text(encoding="utf-8")) == {}


def test_malformed_json_is_unchanged_and_reported(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = "{not json"
    settings.write_text(original, encoding="utf-8")

    result = install_hooks(tmp_path)

    assert result.status == "malformed"
    assert settings.read_text(encoding="utf-8") == original
    assert check_hooks(tmp_path).status == "malformed"


def test_non_object_json_is_malformed_and_unchanged(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("[]", encoding="utf-8")

    result = install_hooks(tmp_path)

    assert result.status == "malformed"
    assert settings.read_text(encoding="utf-8") == "[]"


def test_symlink_is_refused(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    settings = settings_dir / "settings.json"
    try:
        settings.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="symlink"):
        install_hooks(tmp_path)


def test_modified_owned_handler_is_reported(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    install_hooks(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 99
    settings.write_text(json.dumps(data), encoding="utf-8")

    assert check_hooks(tmp_path).status == "modified"


def test_owned_handler_shape_is_exact() -> None:
    assert OWNED_HANDLER == {
        "type": "command",
        "command": "lark-bot",
        "args": ["claude-hook"],
        "async": True,
        "timeout": 10,
    }
