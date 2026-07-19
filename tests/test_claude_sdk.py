from __future__ import annotations

import asyncio
import sys
from dataclasses import fields
from types import ModuleType, SimpleNamespace

import pytest


def test_cli_import_does_not_require_claude_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = __import__

    def rejecting_import(name: str, *args: object, **kwargs: object):
        if name == "claude_agent_sdk":
            raise ModuleNotFoundError("no claude sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", rejecting_import)
    sys.modules.pop("lark_bot.cli", None)
    __import__("lark_bot.cli")


def test_internal_contract_has_json_alias_and_dataclasses() -> None:
    from lark_bot.modules.claude.claude_sdk import (
        ClaudePermissionResult,
        ClaudeSdkOptions,
        ClaudeSdkResult,
        JsonValue,
    )

    assert JsonValue
    assert {field.name for field in fields(ClaudePermissionResult)} == {
        "allowed",
        "updated_input",
        "message",
    }
    assert {field.name for field in fields(ClaudeSdkOptions)} == {
        "cwd",
        "model",
        "permission_mode",
        "resume",
        "session_id",
        "can_use_tool",
    }
    assert {field.name for field in fields(ClaudeSdkResult)} == {
        "session_id",
        "subtype",
        "is_error",
        "duration_ms",
        "result",
        "errors",
    }


def test_bridge_lazily_imports_sdk_and_translates_options_and_callbacks() -> None:
    from lark_bot.modules.claude.claude_sdk import (
        ClaudeAgentSdkBridge,
        ClaudePermissionResult,
        ClaudeSdkOptions,
    )

    observed: dict[str, object] = {}

    class FakePermissionAllow:
        def __init__(self, *, updated_input: dict[str, object] | None = None) -> None:
            self.updated_input = updated_input

    class FakePermissionDeny:
        def __init__(self, *, message: str) -> None:
            self.message = message

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            observed["options"] = kwargs
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, *, options: FakeOptions) -> None:
            observed["client_options"] = options.kwargs

        async def connect(self) -> None:
            observed["connect"] = True

        async def query(self, prompt: str) -> None:
            observed["prompt"] = prompt

        async def receive_response(self):
            if False:
                yield None

        async def interrupt(self) -> None:
            observed["interrupt"] = True

        async def disconnect(self) -> None:
            observed["disconnect"] = True

    sdk = ModuleType("claude_agent_sdk")
    sdk.ClaudeSDKClient = FakeClient
    sdk.ClaudeAgentOptions = FakeOptions
    sdk.PermissionResultAllow = FakePermissionAllow
    sdk.PermissionResultDeny = FakePermissionDeny

    async def callback(tool_name: str, input_data: dict[str, object], context: object):
        observed["callback_args"] = (tool_name, input_data, context)
        return ClaudePermissionResult(allowed=True, updated_input={"safe": True})

    options = ClaudeSdkOptions(
        cwd="/tmp/project",
        model="claude-test",
        permission_mode="default",
        resume="resume-token",
        session_id="local-session",
        can_use_tool=callback,
    )
    bridge = ClaudeAgentSdkBridge(importer=lambda _: sdk)
    client = bridge(options)
    assert observed["options"] == {
        "cwd": "/tmp/project",
        "model": "claude-test",
        "permission_mode": "default",
        "resume": "resume-token",
        "can_use_tool": observed["options"]["can_use_tool"],
    }
    assert "session_id" not in observed["options"]

    async def exercise() -> None:
        callback_wrapper = observed["options"]["can_use_tool"]
        result = await callback_wrapper("Bash", {"command": "pwd"}, SimpleNamespace())
        assert isinstance(result, FakePermissionAllow)
        assert result.updated_input == {"safe": True}
        await client.connect()
        await client.query("do not persist this prompt")
        await client.interrupt()
        await client.close()

    asyncio.run(exercise())
    assert observed["prompt"] == "do not persist this prompt"
    assert observed["disconnect"] is True


def test_bridge_translates_result_messages_without_exposing_sdk_objects() -> None:
    from lark_bot.modules.claude.claude_sdk import ClaudeAgentSdkBridge, ClaudeSdkOptions, ClaudeSdkResult

    class FakeResultMessage:
        def __init__(self) -> None:
            self.session_id = "sdk-session"
            self.subtype = "success"
            self.is_error = False
            self.duration_ms = 123
            self.result = "done"
            self.errors = ["warning"]

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, *, options: FakeOptions) -> None:
            self.options = options

        async def receive_response(self):
            yield FakeResultMessage()

    sdk = ModuleType("claude_agent_sdk")
    sdk.ClaudeSDKClient = FakeClient
    sdk.ClaudeAgentOptions = FakeOptions
    sdk.PermissionResultAllow = type("FakePermissionAllow", (), {})
    sdk.PermissionResultDeny = type("FakePermissionDeny", (), {})
    sdk.ResultMessage = FakeResultMessage
    client = ClaudeAgentSdkBridge(importer=lambda _: sdk)(ClaudeSdkOptions(session_id="local"))

    async def collect() -> list[object]:
        return [message async for message in client.receive_response()]

    messages = asyncio.run(collect())
    assert messages == [
        ClaudeSdkResult(
            session_id="sdk-session",
            subtype="success",
            is_error=False,
            duration_ms=123,
            result="done",
            errors=("warning",),
        )
    ]
    assert type(messages[0]).__module__ == "lark_bot.modules.claude.claude_sdk"


def test_permission_callback_translates_denial_message() -> None:
    from lark_bot.modules.claude.claude_sdk import (
        ClaudeAgentSdkBridge,
        ClaudePermissionResult,
        ClaudeSdkOptions,
    )

    class Allow:
        def __init__(self, *, updated_input=None) -> None:
            self.updated_input = updated_input

    class Deny:
        def __init__(self, *, message: str) -> None:
            self.message = message

    class Options:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    observed: dict[str, object] = {}

    class Client:
        def __init__(self, *, options: Options) -> None:
            observed["options"] = options.kwargs

    sdk = ModuleType("claude_agent_sdk")
    sdk.ClaudeSDKClient = Client
    sdk.ClaudeAgentOptions = Options
    sdk.PermissionResultAllow = Allow
    sdk.PermissionResultDeny = Deny

    async def callback(tool_name, input_data, context):
        assert tool_name == "Write"
        assert input_data == {"path": "safe"}
        assert context == {"permission_suggestions": ["allow"]}
        return ClaudePermissionResult(allowed=False, message="not allowed")

    client = ClaudeAgentSdkBridge(importer=lambda _: sdk)(
        ClaudeSdkOptions(can_use_tool=callback)
    )
    wrapper = observed["options"]["can_use_tool"]

    async def exercise() -> None:
        result = await wrapper(
            "Write",
            {"path": "safe"},
            {"permission_suggestions": ["allow"]},
        )
        assert isinstance(result, Deny)
        assert result.message == "not allowed"

    asyncio.run(exercise())


def test_missing_sdk_raises_only_when_bridge_is_constructed() -> None:
    from lark_bot.modules.claude.claude_sdk import ClaudeAgentSdkBridge, ClaudeSdkOptions

    def missing(_: str):
        raise ModuleNotFoundError("No module named 'claude_agent_sdk'")

    bridge = ClaudeAgentSdkBridge(importer=missing)
    with pytest.raises(ModuleNotFoundError, match="claude_agent_sdk"):
        bridge(ClaudeSdkOptions())
