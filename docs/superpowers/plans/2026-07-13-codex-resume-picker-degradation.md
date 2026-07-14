# Codex Resume Picker Degradation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the unsupported remote Codex session picker from failing after startup, while preserving safe non-picker resume paths and direct native picker access without Lark.

**Architecture:** Add a narrow CLI preflight before daemon session creation. Remote launches reject `resume` when neither `--last` nor an explicit immediate session target is present; `--no-lark` bypasses the restriction. Keep the single-client gateway, but replace its generic second-client close reason with the same actionable guidance and document the verified limitation.

**Tech Stack:** Python 3.11, Typer/Click, asyncio WebSockets, pytest, Markdown.

---

## File Map

- Modify `src/lark_bot/cli.py`: identify picker-dependent resume invocations and fail before contacting the daemon.
- Modify `src/lark_bot/codex_gateway.py`: return an actionable WebSocket close reason for a second terminal client.
- Modify `tests/test_cli_codex.py`: define blocked picker and preserved resume/direct-launch behavior.
- Modify `tests/test_codex_gateway.py`: assert the actionable second-client close reason.
- Modify `README.md`: document supported resume commands and the in-TUI `/resume` limitation.
- Do not modify the app-server, orchestrator, storage schema, or probe decision.

### Task 1: Define the degradation contract with failing tests

**Files:**
- Modify: `tests/test_cli_codex.py`
- Modify: `tests/test_codex_gateway.py`

- [ ] **Step 1: Add failing CLI preflight tests**

Add a shared daemon spy and launcher spy, then cover these exact cases:

```python
def test_codex_resume_picker_is_rejected_before_daemon_session(monkeypatch):
    daemon_calls = []
    launcher_calls = []
    monkeypatch.setattr(
        "lark_bot.cli._daemon_request",
        lambda *args, **kwargs: daemon_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "lark_bot.cli.CodexTuiLauncher.run",
        lambda self, options: launcher_calls.append(options),
    )

    result = CliRunner().invoke(app, ["codex", "resume"])

    assert result.exit_code == 2
    assert "session picker" in result.output.lower()
    assert "resume --last" in result.output
    assert "--no-lark" in result.output
    assert daemon_calls == []
    assert launcher_calls == []


def test_codex_resume_picker_options_are_rejected(monkeypatch):
    monkeypatch.setattr(
        "lark_bot.cli._daemon_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("daemon must not be contacted")
        ),
    )

    for argv in (
        ["codex", "resume", "--all"],
        ["codex", "resume", "--include-non-interactive"],
        ["codex", "resume", "--model", "gpt-test"],
    ):
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 2, argv
        assert "session picker" in result.output.lower(), argv
```

These tests intentionally treat a model option value as an option value, not a
session target. The accepted explicit-target form is `resume <SESSION_ID>`.

- [ ] **Step 2: Add preserved-path tests**

Keep the existing `resume --last` test and add:

```python
def test_codex_resume_explicit_session_is_forwarded(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "lark_bot.cli._daemon_request",
        lambda method, path, **kwargs: (
            {
                "session_id": "s1",
                "endpoint": "ws://127.0.0.1:1",
                "remote_auth_token": "t",
            }
            if method == "POST"
            else None
        ),
    )
    monkeypatch.setattr(
        "lark_bot.cli.CodexTuiLauncher.run",
        lambda self, options: seen.append(options.args) or 0,
    )

    result = CliRunner().invoke(app, ["codex", "resume", "session-name"])

    assert result.exit_code == 0
    assert seen == [["resume", "session-name"]]


def test_codex_no_lark_allows_native_resume_picker(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "lark_bot.cli._daemon_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("daemon must not be contacted")
        ),
    )
    monkeypatch.setattr(
        "lark_bot.cli.CodexTuiLauncher.run",
        lambda self, options: seen.append(options) or 0,
    )

    result = CliRunner().invoke(app, ["codex", "--no-lark", "resume"])

    assert result.exit_code == 0
    assert seen[0].args == ["resume"]
    assert seen[0].remote_endpoint is None
```

- [ ] **Step 3: Strengthen the gateway close-reason test**

Extend `test_rejects_second_terminal_client`:

```python
assert closed.value.rcvd.code == 1008
assert closed.value.rcvd.reason == (
    "session picker unsupported; use resume --last, an explicit session ID, "
    "or --no-lark"
)
```

- [ ] **Step 4: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/test_cli_codex.py tests/test_codex_gateway.py -q
```

Expected failures:

- picker invocations currently contact the daemon and launch Codex;
- explicit picker preflight message does not exist;
- second-client close reason is still `only one terminal client is allowed`.

- [ ] **Step 5: Commit the failing contract**

```powershell
git add tests/test_cli_codex.py tests/test_codex_gateway.py
git commit -m "test: define codex resume picker degradation"
```

### Task 2: Implement the CLI preflight and gateway guidance

**Files:**
- Modify: `src/lark_bot/cli.py`
- Modify: `src/lark_bot/codex_gateway.py`
- Test: `tests/test_cli_codex.py`
- Test: `tests/test_codex_gateway.py`

- [ ] **Step 1: Add a conservative picker detector**

Add constants and a focused helper near `_run_codex_tui`:

```python
REMOTE_RESUME_PICKER_MESSAGE = (
    "The Codex session picker is unavailable through the Lark gateway. "
    "Use `lark-bot codex resume --last`, "
    "`lark-bot codex resume <SESSION_ID>`, or "
    "`lark-bot codex --no-lark resume`."
)


def _uses_remote_resume_picker(args: Sequence[str]) -> bool:
    values = list(args)
    if not values or values[0] != "resume":
        return False
    remainder = values[1:]
    if "--last" in remainder:
        return False
    if remainder and not remainder[0].startswith("-"):
        return False
    return True
```

This intentionally allows the documented explicit-target form only when the
target immediately follows `resume`. It avoids mistaking option values such as
`--model gpt-test` for a session ID. Future Codex options therefore fail safe
instead of accidentally opening the unsupported picker.

- [ ] **Step 2: Fail before settings and daemon access**

At the first line of `_run_codex_tui`:

```python
if not no_lark and _uses_remote_resume_picker(args):
    raise typer.BadParameter(REMOTE_RESUME_PICKER_MESSAGE)
```

Only after this check should the function call `get_settings()` or
`_daemon_request()`.

- [ ] **Step 3: Replace the second-client close reason**

In `CodexGateway._handle_terminal`, preserve status `1008` but use this reason:

```python
SECOND_TERMINAL_CLOSE_REASON = (
    "session picker unsupported; use resume --last, an explicit session ID, "
    "or --no-lark"
)
```

The reason is short enough for a WebSocket close control frame and contains no
endpoint or authentication material.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_cli_codex.py tests/test_codex_gateway.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Run neighboring regression tests**

Run:

```powershell
python -m pytest tests/test_codex_tui.py tests/test_codex_interactive.py tests/test_daemon_core.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit implementation**

```powershell
git add src/lark_bot/cli.py src/lark_bot/codex_gateway.py
git commit -m "fix: reject unsupported codex resume picker"
```

### Task 3: Document and verify the supported workflow

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add resume examples and limitation text**

After the normal interactive launch examples, add:

````markdown
Resume through the Lark gateway without opening the native picker:

```bash
lark-bot codex resume --last
lark-bot codex resume SESSION_ID
```

Codex CLI 0.144.1 cannot keep the primary remote TUI usable after the
secondary picker client closes. Therefore `lark-bot codex resume` without a
target and the in-TUI `/resume` picker are not supported through the Lark
gateway. Exit the TUI and use one of the commands above. To use the native
interactive picker without Lark assistance, run:

```bash
lark-bot codex --no-lark resume
```
````

- [ ] **Step 2: Review documentation safety and accuracy**

Confirm the README:

- does not claim that all resume behavior is transparent;
- distinguishes CLI preflight from the in-TUI limitation;
- does not suggest silently selecting the latest session;
- states the loss of Lark assistance under `--no-lark`;
- contains no local endpoint, token, or machine path.

- [ ] **Step 3: Run full verification**

Run:

```powershell
python -m pytest
git diff --check
git status --short
```

Expected: the full suite passes; only intended files plus the pre-existing
untracked `CLAUDE.md` appear.

- [ ] **Step 4: Commit documentation**

```powershell
git add README.md
git commit -m "docs: explain codex resume picker limitation"
```

### Task 4: Final integrated review

**Files:**
- Inspect all commits from the degradation test contract through documentation.

- [ ] **Step 1: Verify acceptance criteria**

Acceptance requires:

- remote `resume` picker requests fail before daemon session creation;
- `resume --last` and `resume <SESSION_ID>` still use the Lark gateway;
- `--no-lark resume` still opens the native picker directly;
- a second gateway client receives an actionable policy close reason;
- README documents the in-TUI limitation and supported commands;
- the full pytest suite passes.

- [ ] **Step 2: Request final code review**

Review for CLI parsing regressions, accidental daemon calls, WebSocket close
reason size, and documentation consistency. Resolve every Critical or Important
finding before completion.
