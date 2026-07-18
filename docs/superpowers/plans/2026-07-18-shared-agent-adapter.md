# Shared Agent Adapter Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move provider-neutral event parsing, notification construction, and Hook delivery primitives out of the Codex and Claude adapters without changing observable behavior.

**Architecture:** Codex and Claude map raw provider payloads into `AgentNotificationInput`; `notification_builder` creates the shared task, detection, context, and request objects. `agent_event` validates provider event JSON generically, while `agent_hook` handles bounded JSON input and delivery of already-sanitized Hook mappings.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest.

---

### Task 1: Add The Shared Notification Builder

**Files:**
- Modify: `src/lark_bot/modules/notification/notification_model.py`
- Create: `src/lark_bot/modules/notification/notification_builder.py`
- Modify: `src/lark_bot/modules/notification/__init__.py`
- Modify: `tests/test_notification_modules.py`

- [x] **Step 1: Write failing builder tests**

Add tests that construct `AgentNotificationInput` directly and assert:

```python
value = AgentNotificationInput(
    agent=AgentKind.CLAUDE,
    task_name="review",
    session_id="session-1",
    session_name="review",
    event_name="PermissionRequest",
    status=TaskStatus.WAITING_FOR_INPUT,
    command=["claude"],
    summary="allow command",
)
request = build_agent_notification(value)
assert request.context.session_id == "session-1"
assert request.detection.status is TaskStatus.WAITING_FOR_INPUT
assert request.detection.tags == ["claude", "PermissionRequest", "waiting_for_input"]
```

Also assert that output-based waiting elevates success and that non-zero success
becomes failure.

- [x] **Step 2: Verify the tests fail**

Run:

```powershell
python -m pytest tests/test_notification_modules.py -q
```

Expected: import failure for `AgentNotificationInput` or
`notification_builder`.

- [x] **Step 3: Implement the normalized model and builder**

Define `AgentNotificationInput` with provider-neutral fields and implement:

```python
def build_agent_notification(value: AgentNotificationInput) -> NotificationRequest:
    status = value.status
    exit_code = value.exit_code
    if exit_code is None:
        exit_code = 1 if status is TaskStatus.FAILED else 0
    if status is TaskStatus.SUCCEEDED and exit_code != 0:
        status = TaskStatus.FAILED

    output_tail = value.output_tail or ([value.summary] if value.summary else [])
    task = TaskResult(
        name=value.task_name,
        command=value.command,
        exit_code=exit_code,
        duration_seconds=value.duration_seconds,
        stdout_tail=output_tail,
        stderr_tail=value.stderr_tail,
        source=value.agent.value,
    )
    detection = _build_detection(value, task, status)
    context = None
    if value.session_id:
        context = NotificationContext(
            agent=value.agent,
            session_id=value.session_id,
            session_name=value.session_name or value.task_name,
        )
    return NotificationRequest(task=task, detection=detection, context=context)


def _build_detection(
    value: AgentNotificationInput,
    task: TaskResult,
    status: TaskStatus,
) -> DetectionResult:
    detected = detect_output(task.combined_tail_text, task.exit_code)
    event_tags = [value.event_name] if value.event_name else []
    tags = dedupe_tags([value.agent.value, *event_tags, *value.tags])
    if status is TaskStatus.WAITING_FOR_INPUT:
        if TaskStatus.WAITING_FOR_INPUT.value in tags:
            waiting_tags: list[str] = []
        elif detected.status is TaskStatus.WAITING_FOR_INPUT:
            waiting_tags = detected.tags
        else:
            waiting_tags = [TaskStatus.WAITING_FOR_INPUT.value]
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=dedupe_tags([*tags, *waiting_tags]),
            matched_phrases=detected.matched_phrases,
        )
    if detected.status is TaskStatus.WAITING_FOR_INPUT:
        return DetectionResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            tags=dedupe_tags([*tags, *detected.tags]),
            matched_phrases=detected.matched_phrases,
        )
    return DetectionResult(status=status, tags=dedupe_tags([*tags, status.value]))
```

Export the new model and builder from `modules.notification`.

- [x] **Step 4: Verify the builder tests pass**

Run:

```powershell
python -m pytest tests/test_notification_modules.py -q
```

Expected: PASS.

### Task 2: Add Shared Event And Hook Primitives

**Files:**
- Modify: `src/lark_bot/modules/agent/agent_event.py`
- Create: `src/lark_bot/modules/agent/agent_hook.py`
- Modify: `src/lark_bot/modules/agent/__init__.py`
- Create: `tests/test_agent_hooks.py`

- [x] **Step 1: Write failing parser and transport tests**

Cover these public APIs:

```python
assert parse_bounded_json_object('{"event":"Stop"}') == {"event": "Stop"}
assert parse_bounded_json_object("[]") is None
assert parse_bounded_json_object("x" * (MAX_HOOK_BYTES + 1)) is None
assert read_callback_stdin(["hook", '{"event":"Stop"}'], blocking_reader) == ""
```

Verify `deliver_sanitized_hook` calls the sender when available and writes only
the supplied sanitized mapping when the sender raises.

Add a small Pydantic model test proving `parse_event_payload` distinguishes
invalid JSON, non-object JSON, and model validation failures.

- [x] **Step 2: Verify the tests fail**

Run:

```powershell
python -m pytest tests/test_agent_hooks.py -q
```

Expected: import failure for `lark_bot.modules.agent.agent_hook` and
`parse_event_payload`.

- [x] **Step 3: Implement the minimal shared APIs**

Implement:

```python
import json
import uuid

MAX_HOOK_BYTES = 64 * 1024

def parse_bounded_json_object(
    raw: str,
    *,
    max_bytes: int = MAX_HOOK_BYTES,
) -> dict[str, Any] | None:
    try:
        if len(raw.encode("utf-8")) > max_bytes:
            return None
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_callback_stdin(
    argv: Sequence[str],
    reader: Callable[[int], str],
    *,
    max_bytes: int = MAX_HOOK_BYTES,
) -> str:
    if argv and parse_bounded_json_object(argv[-1], max_bytes=max_bytes) is not None:
        return ""
    return reader(max_bytes + 1)


def deliver_sanitized_hook(
    payload: Mapping[str, str],
    sender: Callable[[dict[str, str]], object],
    spool_dir: Path,
) -> bool:
    safe = dict(payload)
    try:
        sender(safe)
        return True
    except Exception:
        try:
            spool_dir.mkdir(parents=True, exist_ok=True)
            path = spool_dir / f"hook-{uuid.uuid4().hex}.json"
            path.write_text(json.dumps(safe, ensure_ascii=False), encoding="utf-8")
            return True
        except OSError:
            return False
```

Add the generic event parser to `agent_event.py`:

```python
def parse_event_payload(
    payload: str,
    model: type[EventModel],
    *,
    provider: str,
) -> EventModel:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"{provider} event payload must be valid JSON.") from error
    if not isinstance(raw, dict):
        raise ValueError(f"{provider} event payload must be a JSON object.")
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise ValueError(f"Invalid {provider} event payload: {error}") from error
```

`deliver_sanitized_hook` catches sender failures, creates the spool directory,
and writes a UUID-named JSON file. It returns false only if spooling fails.

- [x] **Step 4: Verify the shared primitive tests pass**

Run:

```powershell
python -m pytest tests/test_agent_hooks.py -q
```

Expected: PASS.

### Task 3: Migrate Both Providers And Run Regression Tests

**Files:**
- Modify: `src/lark_bot/modules/codex/codex_adapter.py`
- Modify: `src/lark_bot/modules/codex/codex_hook_adapter.py`
- Modify: `src/lark_bot/modules/claude/claude_adapter.py`
- Modify: `src/lark_bot/modules/claude/claude_service.py`
- Modify: `src/lark_bot/commands/common.py`

- [x] **Step 1: Add provider delegation assertions**

Extend adapter tests only where needed to verify that current observable
results remain stable: source, context, status, tags, output tails, and exit
codes. Do not test private implementation details.

- [x] **Step 2: Migrate Codex and Claude notification adapters**

Keep `_normalize_status` and `_event_status` provider-specific. Replace each
adapter's duplicated `TaskResult`, detection, tags, context, and exit-code code
with construction of `AgentNotificationInput` followed by
`build_agent_notification`.

- [x] **Step 3: Migrate JSON parsing and Codex Hook transport**

Use `parse_event_payload` in both JSON-to-notification entrypoints. Keep Codex
payload sanitization and notify chaining in `codex_hook_adapter`, but delegate
bounded object parsing, stdin selection, and sanitized delivery/spooling to
`agent_hook`.

- [x] **Step 4: Run targeted regression tests**

Run:

```powershell
python -m pytest tests/test_agent_hooks.py tests/test_notification_modules.py tests/test_codex_adapter.py tests/test_claude_adapter.py tests/test_codex_hook_adapter.py tests/test_cli_codex.py -q
```

Expected: PASS with unchanged provider-facing behavior.

- [x] **Step 5: Verify dependency direction and full suite**

Run:

```powershell
rg -n "from lark_bot\.modules\.(codex|claude)" src/lark_bot/modules/agent src/lark_bot/modules/task src/lark_bot/modules/notification src/lark_bot/modules/lark
python -m pytest -q
git diff --check
```

Expected: `rg` finds no reverse provider imports, the full suite passes, and
`git diff --check` reports no whitespace errors.

No commit step is included because repository policy requires explicit owner
permission before committing.
