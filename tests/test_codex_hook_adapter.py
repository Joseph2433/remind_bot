from __future__ import annotations

import json

from lark_bot.codex.hook_adapter import (
    MAX_CALLBACK_BYTES,
    forward_existing_notify,
    handle_callback,
    normalize_callback,
    read_stdin_payload,
)


def test_notify_payload_is_read_from_last_argv_and_drops_prompt_and_output():
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "thread-1",
        "turn-id": "turn-1",
        "cwd": "C:/secret/project",
        "input-messages": ["TOP SECRET prompt"],
        "last-assistant-message": "TOP SECRET output",
    }

    safe = normalize_callback(argv=["lark-bot", "codex-hook", json.dumps(payload)], stdin="")

    assert safe == {
        "hook_event_name": "Stop",
        "event_id": "turn-1",
        "thread_id": "thread-1",
        "callback_type": "agent-turn-complete",
    }
    assert "TOP SECRET" not in json.dumps(safe)


def test_stdin_hook_payload_accepts_minimal_verified_fields():
    safe = normalize_callback(
        argv=["lark-bot", "codex-hook"],
        stdin=json.dumps({"hook_event_name": "PermissionRequest", "event_id": "event-1", "prompt": "secret"}),
    )

    assert safe == {"hook_event_name": "PermissionRequest", "event_id": "event-1"}


def test_callback_rejects_oversize_and_non_object_payloads():
    assert normalize_callback(argv=["cmd", "x" * (MAX_CALLBACK_BYTES + 1)], stdin="") is None
    assert normalize_callback(argv=["cmd"], stdin="[]") is None


def test_notify_argv_does_not_read_inherited_terminal_stdin():
    raw = json.dumps({"type": "agent-turn-complete", "turn-id": "turn-1"})

    def blocking_read(_limit):
        raise AssertionError("notify argv must not read terminal stdin")

    assert read_stdin_payload(["codex-hook", raw], blocking_read) == ""


def test_daemon_failure_spools_only_normalized_payload(workspace_tmp_path):
    payload = json.dumps(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "turn-1",
            "input-messages": ["secret prompt"],
            "last-assistant-message": "secret output",
        }
    )

    def unavailable(_safe):
        raise OSError("offline")

    spool = workspace_tmp_path / "spool"
    assert handle_callback(argv=["cmd", payload], stdin="", sender=unavailable, spool_dir=spool)
    files = list(spool.glob("hook-*.json"))
    assert len(files) == 1
    saved = files[0].read_text(encoding="utf-8")
    assert "secret" not in saved
    assert json.loads(saved)["event_id"] == "turn-1"


def test_existing_notify_is_chained_with_original_payload_and_recursion_guard(monkeypatch):
    raw = json.dumps({"type": "agent-turn-complete", "turn-id": "turn-1", "last-assistant-message": "original"})
    calls = []
    monkeypatch.setattr("lark_bot.codex.hook_adapter.subprocess.Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    env = {"LARK_BOT_CODEX_NOTIFY_CHAIN": json.dumps(["existing-notifier", "turn-ended"])}

    assert forward_existing_notify(argv=["codex-hook", raw], stdin="", environ=env)
    assert calls[0][0] == ["existing-notifier", "turn-ended", raw]
    assert calls[0][1]["env"]["LARK_BOT_CODEX_NOTIFY_CHAIN_ACTIVE"] == "1"
    assert not forward_existing_notify(
        argv=["codex-hook", raw],
        stdin="",
        environ={**env, "LARK_BOT_CODEX_NOTIFY_CHAIN_ACTIVE": "1"},
    )
