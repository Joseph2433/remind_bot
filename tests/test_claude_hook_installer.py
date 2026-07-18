from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import lark_bot.modules.claude.claude_hook_installer as installer

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


def test_uninstall_does_not_remove_owned_handler_from_unmanaged_event(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    install_hooks(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["UserPromptSubmit"] = [{"hooks": [OWNED_HANDLER]}]
    settings.write_text(json.dumps(data), encoding="utf-8")

    uninstall_hooks(tmp_path)

    value = json.loads(settings.read_text(encoding="utf-8"))
    assert value["hooks"]["UserPromptSubmit"] == [{"hooks": [OWNED_HANDLER]}]


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


def test_uninstall_prunes_empty_managed_event_and_top_level_hooks(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [], "UserPromptSubmit": []}}),
        encoding="utf-8",
    )

    result = uninstall_hooks(tmp_path)

    assert result.status == "missing"
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "permissions": {"allow": ["Read"]},
        "hooks": {"UserPromptSubmit": []},
    }


def test_uninstall_absent_settings_is_idempotent_without_creating_file(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"

    first = uninstall_hooks(tmp_path)
    second = uninstall_hooks(tmp_path)

    assert first.status == "missing"
    assert second.status == "missing"
    assert not settings.exists()
    assert not settings.parent.exists()


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


def test_invalid_utf8_is_malformed_with_safe_detail_and_unchanged(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = b'{"secret-path": "\xff"}'
    settings.write_bytes(original)

    for operation in (check_hooks, install_hooks, uninstall_hooks):
        result = operation(tmp_path)
        assert result.status == "malformed"
        assert result.detail == "settings must be valid UTF-8"
        assert settings.read_bytes() == original


def test_read_error_detail_does_not_expose_path(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "C:/private/project/settings.json"

    def unavailable(_path: Path, *args: object, **kwargs: object) -> bytes:
        raise OSError(f"access denied: {secret}")

    monkeypatch.setattr(Path, "read_bytes", unavailable)

    result = check_hooks(workspace_tmp_path)

    assert result.status == "malformed"
    assert result.detail == "unable to read settings"
    assert secret not in result.detail


def test_malformed_json_detail_is_fixed_and_safe(workspace_tmp_path: Path) -> None:
    settings = workspace_tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"private":', encoding="utf-8")

    result = check_hooks(workspace_tmp_path)

    assert result.status == "malformed"
    assert result.detail == "settings must be valid JSON"


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


def test_symlinked_claude_directory_is_refused_even_without_os_symlink_privilege(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        return path == settings.parent or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(ValueError, match="symlink"):
        install_hooks(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        uninstall_hooks(tmp_path)


def test_reparse_point_claude_directory_is_refused(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = workspace_tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}", encoding="utf-8")
    real_lstat = os.lstat
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def fake_lstat(path: str | os.PathLike[str]) -> object:
        result = real_lstat(path)
        if Path(path) == settings.parent:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_file_attributes=reparse_flag,
            )
        return result

    monkeypatch.setattr(installer.os, "lstat", fake_lstat)

    with pytest.raises(ValueError, match="reparse"):
        install_hooks(workspace_tmp_path)
    with pytest.raises(ValueError, match="reparse"):
        uninstall_hooks(workspace_tmp_path)


def test_atomic_write_aborts_if_parent_identity_changes(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = workspace_tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = b"{}"
    settings.write_bytes(original)
    real_identity = getattr(installer, "_parent_identity", lambda path: (0, 0, 0, 0))
    stable = real_identity(settings.parent)
    changed = (stable[0], stable[1] + 1, stable[2], stable[3])
    identities = iter((stable, stable, changed))

    def swapped_identity(path: Path) -> tuple[int, int, int, int]:
        if path == settings.parent:
            return next(identities)
        return real_identity(path)

    monkeypatch.setattr(installer, "_parent_identity", swapped_identity, raising=False)

    with pytest.raises(ValueError, match="changed"):
        install_hooks(workspace_tmp_path)

    assert settings.read_bytes() == original
    assert list(settings.parent.glob(f".{settings.name}.*.tmp")) == []


def test_atomic_write_cleans_temp_if_parent_changes_after_creation(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = workspace_tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = b"{}"
    settings.write_bytes(original)
    real_identity = installer._parent_identity
    stable = real_identity(settings.parent)
    changed = (stable[0], stable[1] + 1, stable[2], stable[3])
    identities = iter((stable, changed))

    monkeypatch.setattr(installer, "_parent_identity", lambda _path: next(identities))

    with pytest.raises(ValueError, match="changed"):
        install_hooks(workspace_tmp_path)

    assert settings.read_bytes() == original
    assert list(settings.parent.glob(f".{settings.name}.*.tmp")) == []


def test_install_preserves_existing_target_changed_after_read(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = workspace_tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}", encoding="utf-8")
    user_update = b'{"permissions":{"allow":["Bash"]}}'
    real_write = installer._write_atomic

    def raced_write(path: Path, value: dict[str, object], snapshot: object):
        settings.write_bytes(user_update)
        return real_write(path, value, snapshot)

    monkeypatch.setattr(installer, "_write_atomic", raced_write)

    result = install_hooks(workspace_tmp_path)

    assert result.status == "modified"
    assert result.detail == "settings changed during update"
    assert settings.read_bytes() == user_update
    assert list(settings.parent.glob(f".{settings.name}.*.tmp")) == []


def test_install_preserves_target_created_after_missing_read(
    workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = workspace_tmp_path / ".claude" / "settings.json"
    user_update = b'{"permissions":{"deny":["Write"]}}'
    real_write = installer._write_atomic

    def raced_write(path: Path, value: dict[str, object], snapshot: object):
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_bytes(user_update)
        return real_write(path, value, snapshot)

    monkeypatch.setattr(installer, "_write_atomic", raced_write)

    result = install_hooks(workspace_tmp_path)

    assert result.status == "modified"
    assert result.detail == "settings changed during update"
    assert settings.read_bytes() == user_update
    assert list(settings.parent.glob(f".{settings.name}.*.tmp")) == []


@pytest.mark.parametrize(
    "event_value",
    [
        {},
        ["not-a-matcher-group"],
        [{"hooks": {"type": "command"}}],
    ],
)
def test_invalid_managed_hook_structure_is_malformed_and_unchanged(
    workspace_tmp_path: Path, event_value: object
) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"Stop": event_value}})
    settings.write_text(original, encoding="utf-8")

    assert check_hooks(tmp_path).status == "malformed"
    assert install_hooks(tmp_path).status == "malformed"
    assert settings.read_text(encoding="utf-8") == original
    assert uninstall_hooks(tmp_path).status == "malformed"
    assert settings.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "handler",
    [
        "not-an-object",
        {},
        {"command": "other"},
        {"type": None},
        {"type": ""},
        {"type": "   "},
        {"type": "command", "command": ""},
        {"type": "command", "command": 7},
        {"type": "command", "command": "other", "args": "--bad"},
        {"type": "command", "command": "other", "args": [""]},
        {"type": "command", "command": "other", "args": [7]},
        {"type": "command", "command": "other", "async": "yes"},
        {"type": "command", "command": "other", "timeout": 0},
        {"type": "command", "command": "other", "timeout": -1},
        {"type": "command", "command": "other", "timeout": True},
        {"type": "command", "command": "other", "timeout": "10"},
        {"type": "command", "command": "other", "timeout": float("nan")},
        {"type": "command", "command": "other", "timeout": float("inf")},
    ],
)
def test_invalid_managed_handler_is_malformed_and_unchanged(
    workspace_tmp_path: Path, handler: object
) -> None:
    settings = workspace_tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = json.dumps({"hooks": {"Stop": [{"hooks": [handler]}]}})
    settings.write_text(original, encoding="utf-8")

    for operation in (check_hooks, install_hooks, uninstall_hooks):
        result = operation(workspace_tmp_path)
        assert result.status == "malformed"
        assert settings.read_text(encoding="utf-8") == original


def test_unrelated_dict_handler_type_is_not_overvalidated(workspace_tmp_path: Path) -> None:
    settings = workspace_tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    unrelated = {"type": "prompt", "prompt": {"provider": "other"}}
    settings.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [unrelated]}]}}),
        encoding="utf-8",
    )

    result = install_hooks(workspace_tmp_path)

    assert result.status == "installed"
    value = json.loads(settings.read_text(encoding="utf-8"))
    assert value["hooks"]["Stop"][0]["hooks"] == [unrelated]


def test_install_reports_modified_owned_variant_without_mutation(workspace_tmp_path: Path) -> None:
    tmp_path = workspace_tmp_path
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    variant = dict(OWNED_HANDLER)
    variant["timeout"] = 99
    original = json.dumps({"hooks": {"Stop": [{"hooks": [variant]}]}})
    settings.write_text(original, encoding="utf-8")

    result = install_hooks(tmp_path)

    assert result.status == "modified"
    assert settings.read_text(encoding="utf-8") == original


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
