from __future__ import annotations

import json
from importlib import import_module

import pytest
from pydantic import BaseModel


def test_bounded_hook_parser_accepts_only_json_objects() -> None:
    hook = import_module("lark_bot.modules.agent.agent_hook")

    assert hook.parse_bounded_json_object('{"event":"Stop"}') == {"event": "Stop"}
    assert hook.parse_bounded_json_object("[]") is None
    assert hook.parse_bounded_json_object("not-json") is None
    assert hook.parse_bounded_json_object("x" * (hook.MAX_HOOK_BYTES + 1)) is None


def test_callback_argv_avoids_reading_inherited_stdin() -> None:
    hook = import_module("lark_bot.modules.agent.agent_hook")

    def blocking_reader(_limit: int) -> str:
        raise AssertionError("valid argv JSON must not read inherited stdin")

    assert hook.read_callback_stdin(
        ["hook", '{"event":"Stop"}'],
        blocking_reader,
    ) == ""


def test_callback_without_argv_json_reads_one_bounded_payload() -> None:
    hook = import_module("lark_bot.modules.agent.agent_hook")
    limits: list[int] = []

    def reader(limit: int) -> str:
        limits.append(limit)
        return '{"event":"Stop"}'

    assert hook.read_callback_stdin(["hook"], reader) == '{"event":"Stop"}'
    assert limits == [hook.MAX_HOOK_BYTES + 1]


def test_failed_hook_delivery_spools_only_sanitized_mapping(workspace_tmp_path) -> None:
    hook = import_module("lark_bot.modules.agent.agent_hook")
    spool = workspace_tmp_path / "spool"

    def unavailable(_payload: dict[str, str]) -> None:
        raise OSError("offline")

    safe = {"hook_event_name": "Stop", "event_id": "event-1"}
    assert hook.deliver_sanitized_hook(safe, unavailable, spool)

    files = list(spool.glob("hook-*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == safe


def test_successful_hook_delivery_does_not_create_spool(workspace_tmp_path) -> None:
    hook = import_module("lark_bot.modules.agent.agent_hook")
    spool = workspace_tmp_path / "spool"
    received: list[dict[str, str]] = []

    assert hook.deliver_sanitized_hook(
        {"event_id": "event-1"},
        received.append,
        spool,
    )

    assert received == [{"event_id": "event-1"}]
    assert not spool.exists()


class _ExampleEvent(BaseModel):
    event: str


def test_event_payload_parser_reports_boundary_specific_errors() -> None:
    event_module = import_module("lark_bot.modules.agent.agent_event")
    parser = event_module.parse_event_payload

    assert parser(
        '{"event":"Stop"}',
        _ExampleEvent,
        provider="Claude",
    ) == _ExampleEvent(event="Stop")

    with pytest.raises(ValueError, match="Claude event payload must be valid JSON"):
        parser("not-json", _ExampleEvent, provider="Claude")
    with pytest.raises(ValueError, match="Claude event payload must be a JSON object"):
        parser("[]", _ExampleEvent, provider="Claude")
    with pytest.raises(ValueError, match="Invalid Claude event payload"):
        parser("{}", _ExampleEvent, provider="Claude")
